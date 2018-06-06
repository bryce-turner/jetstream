import os
import sys
import re
import logging
import shlex
import subprocess
import asyncio
import jetstream
from asyncio import (BoundedSemaphore, Event, create_subprocess_shell,
                     create_subprocess_exec)
from asyncio.subprocess import PIPE, STDOUT
from concurrent.futures import CancelledError

log = logging.getLogger(__name__)


class AsyncRunner(object):
    def __init__(self, workflow, backend=None, logging_interval=None):
        self.workflow = workflow
        self.backend = backend
        self.logging_interval = logging_interval or 3

        self._loop = None
        self._pending_tasks = list()
        self._workflow_complete = Event()
        self._workflow_manager = None
        self._logger = None

    def log(self):
        """ Logs a status report """
        log.critical('Workflow status: {}'.format(self.workflow))
        log.critical('Runner status: {} async tasks. {}'.format(
            len(asyncio.Task.all_tasks()), self.backend._concurrency_sem))
        
        if hasattr(self.backend, 'log'):
            self.backend.log()

    async def logger(self):
        log.critical('Logger started!')

        try:
            while not self._workflow_complete.is_set():
                await asyncio.sleep(self.logging_interval)
                self.log()   
        finally:
            log.critical('Logger stopped!')

    async def workflow_manager(self):
        log.critical('Workflow manager started!')

        try:
            for task in self.workflow:
                if task is None:
                    await asyncio.sleep(.1)
                else:
                    await self.spawn(*task)
                await asyncio.sleep(0)
        finally:
            self._workflow_complete.set()

    async def spawn(self, task_id, task_directives):
        log.debug('Spawn: {}'.format((task_id, task_directives)))

        try:
            coro = self.backend.spawn(task_id, task_directives)
            task = asyncio.ensure_future(coro)
            task.add_done_callback(self.handle)
        except Exception as e:
            log.exception(e)
            log.critical('Exception during task spawn, halting run!')
            self._loop.stop()

    def handle(self, task):
        log.debug('Callback: {}'.format(task))

        try:
            task_id, returncode = task.result()

            if returncode is None:
                self.workflow.reset(task_id)
            else:
                self.workflow.send(task_id, returncode)

        except Exception as e:
            log.exception(e)
            log.critical('Exception during result handling, halting run!')
            self._loop.stop()

    def start(self, loop=None):
        log.critical('AsyncRunner with {} starting...'.format(
            self.backend.__class__.__name__))

        if loop is None:
            self._loop = asyncio.get_event_loop()

        manager = self._loop.create_task(self.workflow_manager())
        logger = self._loop.create_task(self.logger())

        if hasattr(self.backend, 'start_coros'):
            coros = self._loop.create_task(self.backend.start_coros())
        else:
            coros = self._loop.create_task(asyncio.sleep(0))

        try:
            self._loop.run_until_complete(manager)
            logger.cancel()
            coros.cancel()
        except KeyboardInterrupt:
            for t in asyncio.Task.all_tasks():
                t.cancel()
        finally:
            self.log()
            log.critical('Event loop shutting down...')
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            log.critical('AsyncRunner stopped!')


class Backend(object):
    """ Backends should implement the coroutine method "spawn" """

    # "spawn" will be called by the asyncrunner when a task is ready
    # for execution. It should be asynchronous and return the task_id
    # and returncode as a tuple: (task_id, returncode)
    def __init__(self, max_concurrency=None):
        self._concurrency_sem = BoundedSemaphore(max_concurrency or guess_concurrency())
        log.critical('Initialized with max concurrency: {}'.format(
            self._concurrency_sem))

    @staticmethod
    def get_out_paths(task_id, task_directives):
        """ Gets stdout and stderr paths from directives, if they are not
        defined, it returns defaults based on the task_id. """
        stdout_path = task_directives.get('stdout')

        if stdout_path is None:
            base_path = os.path.join('logs', re.sub(r'\s', '', task_id))
            stdout_path = base_path + '.out'

        stderr_path = task_directives.get('stderr', stdout_path)

        return stdout_path, stderr_path

    @staticmethod
    def get_stdin_data(task_id, task_directives):
        if 'stdin' in task_directives:
            return task_directives['stdin'].encode()
        else:
            return None

    @staticmethod
    def get_cmd(task_id, task_directives):
        return task_directives.get('cmd')

    async def create_subprocess_shell(self, *args, **kwargs):
        await self._concurrency_sem.acquire()

        try:
            while 1:
                try:
                    return await create_subprocess_shell(*args, **kwargs)
                except (BlockingIOError, OSError) as e:
                    log.critical('Unable to start subprocess: {}'.format(e))
                    log.critical('Retry in 10 seconds.')
                    await asyncio.sleep(10)
        finally:
            self._concurrency_sem.release()

    async def create_subprocess_exec(self, *args, **kwargs):
        await self._concurrency_sem.acquire()

        try:
            while 1:
                try:
                    return await create_subprocess_exec(*args, **kwargs)
                except (BlockingIOError, OSError) as e:
                    log.critical('Unable to start subprocess: {}'.format(e))
                    log.critical('Retry in 10 seconds.')
                    await asyncio.sleep(10)
        finally:
            self._concurrency_sem.release()

    async def spawn(self, *args, **kwargs):
        raise NotImplementedError


class LocalBackend(Backend):
    def __init__(self, *args, **kwargs):
        """ LocalBackend executes tasks as processes on the local machine.
        
        :param max_subprocess: Total number of subprocess allowed regardless of
        cpu requirements. If this is omitted, an estimate of 25% of the system 
        thread limit is used. If child processes in a workflow spawn several 
        threads, the system may start to reject creation of new processes.
        """
        super(LocalBackend, self).__init__(*args, **kwargs)

    async def spawn(self, task_id, task_directives):
        log.debug('LocalBackend spawn: {} {}'.format(task_id, self._concurrency_sem))

        try:
            cmd = self.get_cmd(task_id, task_directives)

            if cmd is None:
                return task_id, 0

            stdin = self.get_stdin_data(task_id, task_directives)
            stdout, stderr = self.get_out_paths(task_id, task_directives)

            if stdout == stderr:
                with open(stdout, 'w') as fp:
                    p = await self.create_subprocess_shell(
                        cmd, stdout=fp, stderr=STDOUT, stdin=PIPE)
            else:
                with open(stdout, 'w') as out, open(stderr, 'w') as err:
                    p = await self.create_subprocess_shell(
                        cmd, stdout=out, stderr=err, stdin=PIPE)

            await p.communicate(input=stdin)
            return task_id, p.returncode

        except CancelledError:
            return task_id, -15


class SlurmBackend(Backend):
    sacct_delimiter = '\037'
    submission_pattern = re.compile(r"Submitted batch job (\d*)")
    job_id_pattern = re.compile(r"(?P<jobid>\d*)\.?(?P<taskid>.*)")

    def __init__(self, max_jobs=None, sacct_frequency=None, chunk_size=None, *args, **kwargs):
        super(SlurmBackend, self).__init__(*args, **kwargs)
        self.sacct_frequency = sacct_frequency or 60
        self.chunk_size = chunk_size or 10000
        self._monitor_jobs = Event()
        self._jobs = {}
        self._jobs_sem = BoundedSemaphore(max_jobs or 1000000)
    
    def log(self):
        log.critical('Slurm jobs: {} {}'.format(len(self._jobs), self._jobs_sem))

    def chunk_jobs(self):
        seq = list(self._jobs.keys())
        size = self.chunk_size
        return (seq[pos:pos + size] for pos in range(0, len(seq), size))

    def add_jid(self, jid):
        job = SlurmBatchJob(jid)
        self._jobs[jid] = job
        return job

    async def start_coros(self):
        log.critical('Slurm job monitor started!')
        self._monitor_jobs.set()

        try:
            while self._monitor_jobs.is_set():
                await asyncio.sleep(self.sacct_frequency)
                await self._update_jobs()
        except CancelledError:
            self._monitor_jobs.clear()
        finally:
            log.critical('Slurm job monitor stopped!')

    async def _update_jobs(self):
        log.critical('Sacct request for {} jobs...'.format(len(self._jobs)))
        sacct_data = {}

        for chunk in self.chunk_jobs():
            data = await self._sacct_request(*chunk)
            sacct_data.update(data)

        log.critical('Status updates for {} jobs'.format(len(sacct_data)))

        reap = set()
        for jid, job in self._jobs.items():
            if jid in sacct_data:
                log.debug('Updating: {}'.format(jid))
                job_data = sacct_data[jid]
                job.update(job_data)

                if job.is_complete:
                    reap.add(jid)
            else:
                log.warning('Unable to get res for {}'.format(job))

        for jid in reap:
            self._jobs.pop(jid)

    async def _sacct_request(self, *job_ids):
        if not job_ids:
            raise ValueError('Missing required argument "job_ids"')

        job_args = ' '.join(['-j {}'.format(jid) for jid in job_ids])

        cmd = 'sacct -P --format all --delimiter={} {}'.format(
            self.sacct_delimiter, job_args)

        log.debug('Launching: {}'.format(cmd))
        p = await self.create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = await p.communicate()

        res = self._parse_sacct(stdout.decode())

        return res

    def _parse_sacct(self, data):
        """ Parse stdout from sacct to a dictionary of jobs and job_data. """
        if not data:
            return {}

        jobs = dict()
        lines = iter(data.splitlines())
        header = next(lines).strip().split(self.sacct_delimiter)

        for line in lines:
            row = dict(zip(header, line.strip().split(self.sacct_delimiter)))
            match = self.job_id_pattern.match(row['JobID'])

            if match is None:
                log.critical('Unable to parse sacct line: {}'.format(line))
                pass

            groups = match.groupdict()
            jobid = groups['jobid']
            taskid = groups['taskid']

            if taskid is '':
                if jobid in jobs:
                    log.critical('Duplicate record for job: {}'.format(jobid))
                else:
                    row['_steps'] = list()
                    jobs[jobid] = row
            else:
                if jobid not in jobs:
                    jobs[jobid] = {'_steps': list()}

                jobs[jobid]['_steps'].append(row)

        log.debug('Parsed data for {} jobs'.format(len(jobs)))
        return jobs

    async def spawn(self, task_id, task_directives):
        log.debug('SlurmBackend spawn: {} {} {}'.format(task_id, 
            self._concurrency_sem, self._jobs_sem))

        await self._jobs_sem.acquire()
        job = None

        try:
            cmd = self.get_cmd(task_id, task_directives)

            if cmd is None:
                return task_id, 0

            cmdline = ' '.join(self.sbatch_cmd(task_id, task_directives))

            p = await self.create_subprocess_shell(
                cmdline, stdout=PIPE, stderr=STDOUT)

            stdout, _ = await p.communicate()
            jid = stdout.decode().strip()

            if p.returncode != 0:
                log.critical('Error launching sbatch: {}'.format(jid))
                await asyncio.sleep(10)
                return task_id, None

            log.critical("Submitted batch job {}".format(jid))

            job = self.add_jid(jid)
            rc = await job.wait()

            return task_id, rc

        except CancelledError:
            if job is not None:
                job.cancel()
            return task_id, -15

        finally:
            self._jobs_sem.release()

    def sbatch_cmd(self, task_id, task_directives):
        """ Returns a formatted sbatch command. """
        args = ['sbatch', '--parsable', '-J', task_id]

        stdout_path, stderr_path = self.get_out_paths(task_id, task_directives)

        if stdout_path != stderr_path:
            args.extend(['-o', stdout_path, '-e', stderr_path])
        else:
            args.extend(['-o', stdout_path])

        if 'cpus' in task_directives and task_directives['cpus']:
            args.extend(['-c', task_directives['cpus']])

        if 'mem' in task_directives and task_directives['mem']:
            args.extend(['--mem', task_directives['mem']])

        if 'time' in task_directives and task_directives['time']:
            args.extend(['-t', task_directives['time']])

        cmd = self.get_cmd(task_id, task_directives)

        if 'stdin' in task_directives and task_directives['stdin']:
            stdin_data = str(task_directives['stdin'])
            formatted_cmd = 'echo \'{}\' | {}'.format(stdin_data, cmd)
            final_cmd = shlex.quote(formatted_cmd)
        else:
            final_cmd = shlex.quote(cmd)

        args.extend(['--wrap', final_cmd])

        return args


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

    def __init__(self, jid):
        self.jid = str(jid)
        self._job_data = None
        self._returncode = None
        self._is_complete = Event()

    @property
    def is_complete(self):
        return self._is_complete.is_set()

    @property
    def job_data(self):
        return self._job_data

    @job_data.setter
    def job_data(self, value):
        self._job_data = value
        state = self._job_data.get('State', '')

        if state not in self.active_states:
            try:
                self.returncode = self.job_data['ExitCode'].partition(':')[0]
            except KeyError:
                self.returncode = -123

    def update(self, job_data):
        self.job_data = job_data

    @property
    def returncode(self):
        return self._returncode

    @returncode.setter
    def returncode(self, value):
        self._returncode = int(value)
        self._is_complete.set()

    async def wait(self):
        try:
            await self._is_complete.wait()
        except CancelledError:
            self.cancel()

        return self.returncode

    def cancel(self):
        log.critical('Scancel: {}'.format(self.jid))
        cmd_args = ('scancel', self.jid)
        return subprocess.call(cmd_args)


def test_async_runner(ntasks=5, backend=LocalBackend):
    wf = jetstream.Workflow()

    wf.add_task('stdin_test',
                cmd='bash -v',
                stdin='#!/bin/bash\necho hello world\nhostname')

    for i in range(ntasks):
        wf.add_task(str(i), cmd='sleep 30 && hostname')

    ar = AsyncRunner(wf, backend=backend)
    ar.start()


def guess_concurrency(default=500):
    try:
        res = int(0.25 * int(subprocess.check_output('ulimit -u', shell=True)))
        return res
    except FileNotFoundError as e:
        log.exception(e)
        return default
