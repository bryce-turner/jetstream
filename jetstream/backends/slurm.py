import re
import shlex
import time
import json
import subprocess
import itertools
import tempfile
import shutil
import asyncio
from datetime import datetime, timedelta
from asyncio.subprocess import PIPE
from concurrent.futures import CancelledError
from jetstream import log
from jetstream.backends import BaseBackend

sacct_delimiter = '\037'
job_id_pattern = re.compile(r"^(?P<jobid>\d+)(_(?P<arraystepid>\d+))?(\.(?P<stepid>(\d+|batch|extern)))?$")


class SlurmBackend(BaseBackend):
    """SlurmBackend will spawn tasks using a Slurm batch scheduler.

    The spawn coroutine will return when the slurm job ID for a task is
    complete. This works by maintaining a dict of SlurmBatchJobs, and
    periodically asking for updates from sacct."""
    count = itertools.count()
    respects = ('cmd', 'stdin', 'stdout', 'stderr', 'cpus', 'mem', 'walltime',
                'slurm_args')

    def __init__(self, runner, max_concurrency=9001, sacct_frequency=60,
                 sbatch=None, sbatch_delay=0.1):
        """SlurmBackend submits tasks as jobs to a Slurm batch cluster

        :param sacct_frequency: Frequency in seconds that job updates will
        be requested from sacct
        :param sbatch: path to the sbatch binary if not on PATH
        """
        super(SlurmBackend, self).__init__(runner)
        self.sbatch = sbatch
        self.sacct_frequency = sacct_frequency
        self.sbatch_delay = sbatch_delay
        self.max_concurrency = max_concurrency
        self.jobs = dict()
        self.coroutines = (self.job_monitor,)
        self._next_update = datetime.now()

        if self.sbatch is None:
            self.sbatch = shutil.which('sbatch') or 'sbatch'

        subprocess.run([self.sbatch, '--version'], check=True)
        log.info('SlurmBackend with {} max jobs'.format(self.max_concurrency))

    def _bump_next_update(self):
        d = timedelta(seconds=self.sacct_frequency)
        self._next_update = datetime.now() + d
        log.debug(f'Next sacct update at {self._next_update.isoformat()}')

    async def wait_for_next_update(self):
        while datetime.now() < self._next_update:
            sleep_delta = self._next_update - datetime.now()
            sleep_seconds = max(0, sleep_delta.total_seconds())
            await asyncio.sleep(sleep_seconds)

    async def job_monitor(self):
        """Request job data updates from sacct for each job in self.jobs."""
        log.info('Slurm job monitor started!')

        try:
            while 1:
                await self.wait_for_next_update()
                self._bump_next_update()

                if not self.jobs:
                    log.debug('No current jobs to check')
                    continue

                sacct_data = sacct(*self.jobs, return_data=True)

                for jid, data in sacct_data.items():
                    if jid in self.jobs:
                        job = self.jobs[jid]
                        job.job_data = data

                        if job.is_done():
                            job.event.set()
                            self.jobs.pop(jid)
        except CancelledError:
            if self.jobs:
                log.info('Requesting scancel for outstanding slurm jobs')
                subprocess.run(['scancel'] + list(self.jobs.keys()))
        finally:
            log.info('Slurm job monitor stopped!')

    def slurm_job_name(self, task):
        """The slurm backend gives each job a name that is
        <run_id>.<job number>
        """
        count = next(self.count)
        run_id = self.runner.run_id
        return '{}.{}'.format(run_id, count)

    def slurm_job_comment(self, task):
        """Slurm jobs will receive a comment that contains details about the
        task, run id, and tags taken from the task directives. If tags are a
        string, they will be converted to a list with shlex.split"""
        tags = task.directives().get('tags', [])

        if isinstance(tags, str):
            tags = shlex.split(tags)

        comment = {
            'run': self.runner.run_id,
            'task': {
                'tid': task.tid,
                'tags': tags,
                }
            }

        comment_string = json.dumps(comment, sort_keys=True)

        if len(comment_string) > 1024:
            return json.dumps({
                'tid': task.tid,
                'err': 'Job comment too long!'
            }, sort_keys=True)
        else:
            return comment_string

    async def spawn(self, task):
        log.debug('Spawn: {}'.format(task))

        if not task.directives().get('cmd'):
            return 0

        time.sleep(self.sbatch_delay) # sbatch breaks when called too frequently

        self._bump_next_update()
        stdin, stdout, stderr = self.get_fd_paths(task)

        job = sbatch(
            cmd=task.directives()['cmd'],
            name=self.slurm_job_name(task),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            comment=self.slurm_job_comment(task),
            cpus_per_task=task.directives().get('cpus'),
            mem=task.directives().get('mem'),
            walltime=task.directives().get('walltime'),
            additional_args=task.directives().get('sbatch_args')
        )

        log.info(f'SlurmBackend submitted({job.jid}): {task.tid}')
        task.state.update(slurm_job_id=job.jid, slurm_args=job.args)

        event = asyncio.Event(loop=self.runner.loop)
        job.event = event
        self.jobs[job.jid] = job

        try:
            await event.wait()

            if job.is_ok():
                log.info(f'Complete: {task.tid}')
                task.complete(job.returncode())
            else:
                log.info(f'Failed: {task.tid}')
                task.state['slurm'] = job.job_data.copy()
                task.fail(job.returncode())

        except asyncio.CancelledError:
            job.cancel()
            task.state['err'] = 'Runner cancelled Backend.spawn'
            task.fail(-15)


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
        sacct_output = launch_sacct(*chunk)
        data.update(sacct_output)

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
           additional_args=None, sbatch=None):
    if sbatch is None:
        sbatch = 'sbatch'

    args = [sbatch, '--parsable']

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
