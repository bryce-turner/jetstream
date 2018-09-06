import re
import shlex
import time
import json
import subprocess
import itertools
import tempfile
import asyncio
from asyncio.subprocess import PIPE
from concurrent.futures import CancelledError
from jetstream import log
from jetstream.backends import BaseBackend

sacct_delimiter = '\037'
job_id_pattern = re.compile(r"^(?P<jobid>\d+)(_(?P<arraystepid>\d+))?(\.(?P<stepid>(\d+|batch)))?$")


class SlurmBackend(BaseBackend):
    """SlurmBackend will spawn tasks using a Slurm batch scheduler.

    The spawn coroutine will return when the slurm job ID for a task is
    complete. This works by maintaining a dict of SlurmBatchJobs, and
    periodically asking for updates from sacct."""
    count = itertools.count()
    respects = ('cmd', 'stdin', 'stdout', 'stderr', 'cpus', 'mem', 'walltime',
                'slurm_args')

    def __init__(self, max_concurrency=9001, sacct_frequency=10):
        """SlurmBackend submits tasks as jobs to a Slurm batch cluster

        :param sacct_frequency: Frequency in seconds that job updates will
        be requested from sacct
        :param chunk_size: Number of jobs that will be checked with each
        request to sacct
        """
        self.sacct_frequency = sacct_frequency
        self.max_concurrency = max_concurrency
        self.jobs = dict()
        log.info('SlurmBackend with {} max jobs'.format(max_concurrency))

    def status(self):
        return 'Slurm jobs: {}'.format(self.semaphore)

    async def coro(self):
        log.info('Slurm job monitor started!')

        try:
            while 1:
                await asyncio.sleep(self.sacct_frequency)
                await self._update_jobs()
        except CancelledError:
            if self.jobs:
                log.info('Requesting scancel for outstanding slurm jobs')
                subprocess.run(['scancel'] + list(self.jobs.keys()))
        finally:
            log.info('Slurm job monitor stopped!')

    async def _update_jobs(self):
        """Request job data updates from sacct for each job in self.jobs."""
        log.verbose('Sacct request for {} jobs...'.format(len(self.jobs)))

        if not self.jobs:
            return

        sacct_data = sacct(*self.jobs, return_data=True)

        for jid, data in sacct_data.items():
            if jid in self.jobs:
                job = self.jobs[jid]
                job.job_data = data

                if job.is_done():
                    job.event.set()
                    self.jobs.pop(jid)

    def slurm_job_name(self, task):
        count = next(self.count)
        run_id = self.runner.fp.id
        return '{}.{}'.format(run_id, count)

    def slurm_job_comment(self, task):
        run = self.runner.fp.serialize()
        tags = task.directives.get('tags', [])

        if isinstance(tags, str):
            tags = shlex.split(tags)

        comment = {
            'run': run,
            'task': {
                'tid': task.tid,
                'tags': tags,
                }
            }

        return json.dumps(comment, sort_keys=True)

    async def spawn(self, task):
        log.debug('Spawn: {}'.format(task))

        if not task.directives.get('cmd'):
            return 0

        time.sleep(.1) # sbatch breaks when called too frequently

        stdin, stdout, stderr = self.get_fd_paths(task)

        job = sbatch(
            cmd=task.directives['cmd'],
            name=self.slurm_job_name(task),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            comment=self.slurm_job_comment(task),
            cpus_per_task=task.directives.get('cpus'),
            mem=task.directives.get('mem'),
            walltime=task.directives.get('walltime'),
            additional_args=task.directives.get('sbatch_args')
        )

        task.set_state(slurm_job_id=job.jid, slurm_args=job.args)

        event = asyncio.Event(loop=self.runner.loop)
        job.event = event
        self.jobs[job.jid] = job

        await event.wait()

        return task.done(job.returncode())


class SlurmBatchJob(object):
    states = {
        'BOOT_FAIL': 'Job terminated due to launch failure, typically due to a '
                     'hardware failure (e.g. unable to boot the node or block '
                     'and the job can not be requeued).',
        'CANCELLED': 'Job was explicitly cancelled by the user or system '
                     'administrator. The job may or may not have been '
                     'initiated.',
        'COMPLETED': 'Job has terminated all processes on all nodes with an '
                     'exit code of zero.',
        'CONFIGURING': 'Job has been allocated resources, but are waiting for '
                       'them to become ready for use (e.g. booting).',
        'COMPLETING': 'Job is in the process of completing. Some processes on '
                      'some nodes may still be active.',
        'FAILED': 'Job terminated with non-zero exit code or other failure '
                  'condition.',
        'NODE_FAIL': 'Job terminated due to failure of one or more allocated '
                     'nodes.',
        'PENDING': 'Job is awaiting resource allocation.',
        'PREEMPTED': 'Job terminated due to preemption.',
        'REVOKED': 'Sibling was removed from cluster due to other cluster '
                   'starting the job.',
        'RUNNING': 'Job currently has an allocation.',
        'SPECIAL_EXIT': 'The job was requeued in a special state. This state '
                        'can be set by users, typically in EpilogSlurmctld, if '
                        'the job has terminated with a particular exit value.',
        'STOPPED': 'Job has an allocation, but execution has been stopped with '
                   'SIGSTOP signal. CPUS have been retained by this job.',
        'SUSPENDED': 'Job has an allocation, but execution has been suspended '
                     'and CPUs have been released for other jobs.',
        'TIMEOUT': 'Job terminated upon reaching its time limit.'
    }

    active_states = {'CONFIGURING', 'COMPLETING', 'RUNNING', 'SPECIAL_EXIT',
                     'PENDING'}

    inactive_states = {'BOOT_FAIL', 'CANCELLED', 'COMPLETED', 'FAILED',
                       'NODE_FAIL', 'PREEMPTED', 'REVOKED',
                       'STOPPED', 'SUSPENDED', 'TIMEOUT'}

    failed_states = {'BOOT_FAIL', 'CANCELLED', 'FAILED', 'NODE_FAIL'}

    passed_states = {'COMPLETED'}

    def __init__(self, jid=None, data=None):
        self.args = None
        self._job_data = None

        if data:
            if jid is None:
                self.jid = str(data['JobID'])
            self._update_state(data)
        else:
            self.jid = str(jid)

    def __eq__(self, other):
        try:
            if other == self.jid:
                return True
        except AttributeError:
            return other.jid == self.jid

    def __repr__(self):
        return '<SlurmBatchJob: {}>'.format(self.jid)

    def _update_state(self, job_data):
        self._job_data = job_data

    def update(self):
        data = launch_sacct(self.jid)

        if not self.jid in data:
            raise ValueError('No job data found for:  {}'.format(self.jid))
        else:
            self.job_data = data[self.jid]

    def wait(self, *args, **kwargs):
        return wait(self.jid, *args, **kwargs)

    @property
    def job_data(self):
        return self._job_data

    @job_data.setter
    def job_data(self, value):
        self._update_state(value)

    def returncode(self):
        """Attempts to returns a standard integer exit code based on Slurm
        "derived" exit code, but falls back to some dumb heuristics if the
        exit code isn't parsing."""
        if not self.is_done():
            raise ValueError('Job not done yet')

        try:
            return int(self.job_data['ExitCode'].partition(':')[0])
        except (KeyError, IndexError):
            if self.is_ok():
                return 0
            else:
                return 1

    def cancel(self):
        log.info('Launching "scancel {}"'.format(self.jid))
        cmd_args = ('scancel', self.jid)
        return subprocess.call(cmd_args)

    def is_done(self):
        if self._job_data:
            state = self._job_data.get('State')

            if state not in self.active_states:
                return True

        return False

    def is_ok(self):
        if not self.is_done():
            raise ValueError('Job is not complete yet.')

        if self.job_data['State'] in self.passed_states:
            return True
        else:
            return False


def wait(*job_ids, update_frequency=10):
    """Wait for one or more slurm batch jobs to complete"""
    while 1:
        jobs = sacct(*job_ids)

        if all([j.is_done() for j in jobs]):
            return
        else:
            time.sleep(update_frequency)


def sacct(*job_ids, chunk_size=1000, strict=False, return_data=False):
    """Query sacct for job records.

    Jobs are returned for each job id, but steps will be combined under a
    single job id object. This will return a placeholder job for any job
    id given, regardless of whether job data was returned by sacct. The
    strict option can be used to raise an error when job data is missing
    for any of the job ids."""
    if not job_ids:
        raise ValueError('Missing required argument: job_ids')

    job_ids = [str(jid) for jid in job_ids]
    jobs = [SlurmBatchJob(jid) for jid in job_ids]

    data = {}
    for i in range(0, len(job_ids), chunk_size):
        chunk = job_ids[i: i + chunk_size]
        data.update(launch_sacct(*chunk))

    log.verbose('Status updates for {} jobs'.format(len(data)))

    if return_data:
        return data

    for job in jobs:
        if not job.jid in data:
            if strict:
                raise ValueError('No records returned for {}'.format(job.jid))
            else:
                log.debug('No records found for {}'.format(job.jid))
        else:
            job.job_data = data[job.jid]

    return jobs


def launch_sacct(*job_ids, delimiter=sacct_delimiter, raw=False):
    """Launch sacct command and return stdout data

    This function returns raw query results, sacct() will be more
    useful in many cases.

    :param job_ids: Job ids to include in the query
    :param delimiter: Delimiter to separate parsable results data
    :param raw: Return raw stdout instead of parsed
    :return: Dict or Bytes
    """
    log.verbose('Sacct request for {} jobs...'.format(len(job_ids)))
    args = ['sacct', '-P', '--format', 'all', '--delimiter={}'.format(delimiter)]

    for jid in job_ids:
        args.extend(['-j', str(jid)])

    log.verbose('Launching: {}'.format(' '.join([shlex.quote(r) for r in args])))
    p = subprocess.run(args, stdout=PIPE, check=True)

    if raw:
        return p.stdout.decode()

    return parse_sacct(p.stdout.decode(), delimiter=delimiter)


def parse_sacct(data, delimiter=sacct_delimiter, id_pattern=job_id_pattern):
    """Parse stdout from sacct to a dictionary of job ids and data."""
    jobs = dict()
    lines = iter(data.strip().splitlines())
    header = next(lines).strip().split(delimiter)

    for line in lines:
        row = dict(zip(header, line.strip().split(delimiter)))

        try:
            match = id_pattern.match(row['JobID'])
            groups = match.groupdict()
        except (KeyError, AttributeError):
            # Job data restrictions are very loose - there is a small chance
            # that the chosen delimiter was added to some field in the job
            # data, and that will break this parser. If that happens records
            # are skipped and a warning is issued, but parsing continues.
            log.warning('Error parsing sacct line: {}'.format(line))
            continue

        # Slurm job ids are <jobid>[_<arrayid>][.<taskid>]. The goal here
        # is to group all job steps (tasks, array steps) under their
        # corresponding jid. The steps are added to a list under the key
        # "_steps", all other data updates the dictionary.
        jid = groups['jobid']

        if groups['stepid'] or groups['arraystepid']:
            if jid not in jobs:
                jobs[jid] = {'_steps': list()}

            jobs[jid]['_steps'].append(row)
        else:
            if jid not in jobs:
                jobs[jid] = dict()

            row['_steps'] = list()
            jobs[jid].update(row)

    return jobs


def sbatch(cmd, name=None, stdin=None, stdout=None, stderr=None, tasks=None,
           cpus_per_task=None, mem=None, walltime=None, comment=None,
           additional_args=None):
    args = ['sbatch', '--parsable']

    if name:
        args.extend(['-J', name])

    if stdin:
        args.extend(['--input', stdin])

    if stdout:
        args.extend(['-o', stdout])

    if stderr:
        args.extend(['-e', stdout])

    if tasks:
        args.extend(['-n', tasks])

    if cpus_per_task:
        args.extend(['-c', cpus_per_task])

    if mem:
        args.extend(['--mem', mem])

    if walltime:
        args.extend(['-t', walltime])

    if comment:
        args.extend(['--comment', comment])

    if additional_args:
        if isinstance(additional_args, str):
            args.append(additional_args)
        else:
            args.extend(additional_args)

    if cmd.startswith('#!'):
        script = cmd
    else:
        script = '#!/bin/bash\n{}'.format(cmd)

    temp = tempfile.NamedTemporaryFile()
    with open(temp.name, 'w') as fp:
        fp.write(script)

    args.append(temp.name)
    args = [str(r) for r in args]

    p = subprocess.run(args, stdout=subprocess.PIPE, check=True)

    jid = p.stdout.decode().strip()
    job = SlurmBatchJob(jid)
    job.args = args
    job.script = script

    return job