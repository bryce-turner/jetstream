import logging
import asyncio
import jetstream
from jetstream.backends import BaseBackend
from asyncio import BoundedSemaphore, create_subprocess_shell, CancelledError
import os
import sys
import requests
import json
from random import randint
import re
from time import sleep
import sys
import tempfile
import glob
import subprocess

log = logging.getLogger('jetstream.local')
cjs_dir_path = "/home/avidalto/projects/2020/jetstream/jetstream-may/cjs-backend/"
api_key = "649cec38be4772725c5c06d4b0e4d494"
pool_name = "linuxpool"

# sys.path.append(cjs_dir_path)
# import cjs

def get_pool_info(pool_name, api_key, pworks_url="http://beta.parallel.works"):
    rq = requests.get('{pworks_url}/api/resources?key={api_key}'.format(pworks_url=pworks_url, api_key=api_key))
    for pool_data in rq.json():
        if pool_data['name'] == pool_name:
            log.info('Got pooldata: {}'.format(pool_data))
            return {
                'serviceport': pool_data['info']['ports']['serviceport'],
                'controlport': pool_data['info']['ports']['controlport'],
                'maxworkers': int(pool_data['settings']['max']),
                'cpus': int(pool_data['info']['cpuPerWorker']) // 2
            }

# pool_info = get_pool_info(pool_name, api_key)
# print("Pool Info:")
# print(pool_info)
# service_url = "http://beta.parallel.works:{}".format(pool_info["serviceport"])



# Same function as in workflow.py jetstream script
def compile(pattern):
    return re.compile('^{}$'.format(pattern))

# Finds the files that match a regex pattern
def expand_regex_path(patterns):
    pfiles = []
    for pattern in patterns:
        if pattern.startswith("~"):
            pattern = os.path.expanduser("~") + pattern[1:]
        if pattern.startswith("/"):
            rootdir = ""
            for subdir in pattern.split("/")[1:]:
                if os.path.isdir(rootdir + "/" + subdir):
                    rootdir = rootdir + "/" + subdir
                else:
                    break
        else:
            rootdir = "./"
        cpattern = compile(pattern)
        for root, dirs, files in os.walk(rootdir):
            for f in files:
                path = root + "/" + f
                if rootdir == "./" and pattern[0:2] != "./":
                    path = path[2:]
                if cpattern.match(path):
                    pfiles.append(path)
    return pfiles



# Added by Alvaro: cpus = maxworkers
class CloudSwiftBackend(BaseBackend):
    def __init__(self, pool_name=None, api_key=None, cpus=None, blocking_io_penalty=None):
        """The LocalBackend executes tasks as processes on the local machine.

        This contains a semaphore that limits tasks by the number of cpus
        that they require. It requires that self.runner be set to get the
        event loop, so it's not instantiated until preflight.

        :param cpus: If this is None, the number of available CPUs will be
            guessed. This cannot be changed after starting the backend.
        :param blocking_io_penalty: Delay (in seconds) when a BlockingIOError
            prevents a new process from spawning.
        :param max_concurrency: Max concurrency limit
        """
        super().__init__()
        api_key = "649cec38be4772725c5c06d4b0e4d494"
        # pool_name = "azurepool"
        pool_name = "linuxpool"
        self.pool_info = get_pool_info(pool_name, api_key)
        log.info('Pool info: {}'.format(self.pool_info))
        self.cpus = self.pool_info['cpus'] * self.pool_info['maxworkers']
        self.bip = blocking_io_penalty \
                   or jetstream.settings['backends']['local']['blocking_io_penalty'].get(int)
        self._cpu_sem = BoundedSemaphore(int(self.cpus))
        self.project_dir = os.getcwd()
        
        # Make directory for cjs launch scripts
        self.cloud_scripts_dir = os.path.join(self.project_dir, 'cloud_scripts')
        os.makedirs(self.cloud_scripts_dir, exist_ok=True)
        
        log.info(f'CloudSwiftBackend initialized with {self.cpus} cpus')

    async def spawn(self, task):
        log.debug('Spawn: {}'.format(task))
        if task.directives.get('cloud-args', dict()).get('local-task'):
            log.info('{} is a local task'.format(task.name))
            return await self.spawn_local(task)
        log.info('{} is a cloud task'.format(task.name))
        return await self.spawn_cloud(task)

    async def spawn_cloud(self, task):
        if 'cmd' not in task.directives:
            return task.complete()
        # Modified by Alvaro: {
        #   To prevent shell from replacing the command locally
        cmd = task.directives['cmd'] #.replace("$","\$")
        # }
        cpus = task.directives.get('cpus', 0)
        cpus_reserved = 0
        open_fps = list()

        # Modified by Alvaro: {
        # if cpus > self.cpus:
        if cpus > self.pool_info['cpus']:
            #raise RuntimeError('Task cpus greater than available cpus')
            raise RuntimeError(
                'Task cpus ({}) greater than available cpus ({}) in worker'.format(cpus, self.pool_info['cpus'])
            )
        # }
        try:
            # Modified by Alvaro: {
            #for i in range(task.directives.get('cpus', 0)):
            #    print(cpu_reserved)
            #for i in range(int(pool_info["maxworkers"])):
            #    await self._cpu_sem.acquire()
            #    cpus_reserved += 1

            stdin, stdout, stderr = self.get_fd_paths(task)

            if stdin:
                stdin_fp = open(stdin, 'r')
                open_fps.append(stdin_fp)
            else:
                stdin_fp = None

            if stdout:
                stdout_fp = open(stdout, 'w')
                open_fps.append(stdout_fp)
            else:
                stdout_fp = None

            if stderr:
                stderr_fp = open(stderr, 'w')
                open_fps.append(stderr_fp)
            else:
                stderr_fp = None
                
            # Write out script to send to the cloud
            log.debug('Writing cloud script for {}'.format(task.name))
            cmd_sh_path = os.path.join(self.cloud_scripts_dir, './{}.sh'.format(task.name))
            # cmd_sh_path = os.getcwd() + "/./" + task.name + ".sh"
            with open(cmd_sh_path, 'w') as cmd_sh_out:
                cmd_sh_out.write(cmd)
            
            cjs_inputs = (
                task.directives['input']
                + expand_regex_path(task.directives['input-re'])
                # + task.directives.get('referenceinput', list())
                + [cmd_sh_path]
            )
            cjs_cmd = get_cjs_cmd(
                cmd="bash {}.sh".format(task.name),
                service_url='http://beta.parallel.works:{}'.format(self.pool_info['serviceport']),
                inputs=cjs_inputs,
                outputs=task.directives["output"]
            )

            with open('cjs_cmds_debug.sh', 'w') as cjs_log:
                cjs_log.write('{}\n'.format(cjs_cmd))
            
            p = await self.subprocess_sh(
                cjs_cmd,
                stdin=stdin_fp,
                stdout=stdout_fp,
                stderr=stderr_fp
            )

            task.state.update(
                stdout_path=stdout,
                stderr_path=stderr,
                label=f'Slurm({p.pid})',
            )

            log.info(f'CloudSwiftBackend spawned({p.pid}): {task.name}')
            rc = await p.wait()

            if rc != 0:
                log.info(f'Failed: {task.name}')
                return task.fail(p.returncode)
            else:
                log.info(f'Complete: {task.name}')
                return task.complete(p.returncode)
        except CancelledError:
            task.state['err'] = 'Runner cancelled Backend.spawn()'
            return task.fail(-15)
        finally:
            for fp in open_fps:
                fp.close()

            for i in range(cpus_reserved):
                self._cpu_sem.release()

            return task
    
    async def spawn_local(self, task):
        if 'cmd' not in task.directives:
            return task.complete()

        cmd = task.directives['cmd']
        cpus = task.directives.get('cpus', 0)
        cpus_reserved = 0
        open_fps = list()

        if cpus > self.cpus:
            raise RuntimeError('Task cpus greater than available cpus')

        try:
            for i in range(task.directives.get('cpus', 0)):
                await self._cpu_sem.acquire()
                cpus_reserved += 1

            stdin, stdout, stderr = self.get_fd_paths(task)

            if stdin:
                stdin_fp = open(stdin, 'r')
                open_fps.append(stdin_fp)
            else:
                stdin_fp = None

            if stdout:
                stdout_fp = open(stdout, 'w')
                open_fps.append(stdout_fp)
            else:
                stdout_fp = None

            if stderr:
                stderr_fp = open(stderr, 'w')
                open_fps.append(stderr_fp)
            else:
                stderr_fp = None

            p = await self.subprocess_sh(
                cmd,
                stdin=stdin_fp,
                stdout=stdout_fp,
                stderr=stderr_fp
            )

            task.state.update(
                stdout_path=stdout,
                stderr_path=stderr,
                label=f'Slurm({p.pid})',
            )

            log.info(f'LocalBackend spawned({p.pid}): {task.name}')
            rc = await p.wait()

            if rc != 0:
                log.info(f'Failed: {task.name}')
                return task.fail(p.returncode)
            else:
                log.info(f'Complete: {task.name}')
                return task.complete(p.returncode)
        except CancelledError:
            task.state['err'] = 'Runner cancelled Backend.spawn()'
            return task.fail(-15)
        finally:
            for fp in open_fps:
                fp.close()

            for i in range(cpus_reserved):
                self._cpu_sem.release()

            return task

    async def subprocess_sh(
            self, args, *, stdin=None, stdout=None, stderr=None,
            cwd=None, encoding=None, errors=None, env=None,
            loop=None, executable="/bin/bash"):
        """Asynchronous version of subprocess.run

        This will always use a shell to launch the subprocess, and it prefers
        /bin/bash (can be changed via arguments)"""
        log.debug(f'subprocess_sh:\n{args}')
        #print("{} {}".format(executable, args))
        while 1:
            try:
                p = await create_subprocess_shell(
                    args,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=cwd,
                    encoding=encoding,
                    errors=errors,
                    env=env,
                    loop=loop,
                    executable=executable
                )
                break
            except BlockingIOError as e:
                log.warning(f'System refusing new processes: {e}')
                await asyncio.sleep(self.bip)

        return p














def replace_root_dir(path, root_dir):
    if not root_dir.endswith("/"):
        root_dir = root_dir + "/"
    if "./" in path:
        return root_dir + path.split("./")[1]
    else:
        if path.startswith("/"):
            return path
        else:
            return root_dir + os.path.basename(path) # path

# Inputs: Local path and remote working directotry
# Outputs: Local to remote mapping
def map_stage_in(lpath, rwd):
    if "->" in lpath:
        return lpath

    rpath = replace_root_dir(lpath, rwd)
    return "{} -> {}".format(lpath, rpath)

# Inputs: Remote path and local directory
# Outputs: Local to remote mapping
def map_stage_out(rpath, lwd = None):
    if "<-" in rpath:
        return rpath

    if lwd is None:
        lwd = os.getcwd()
    lpath = replace_root_dir(rpath, lwd)
    os.makedirs(os.path.dirname(lpath), exist_ok=True)
    return "{} <- {}".format(lpath, rpath)

def get_cjs_cmd(cmd, service_url, inputs = [], outputs = [], stdout = None, stderr = None, redirected = False, rwd = None):
    if rwd is None:
        rwd = "/tmp/pworks/" + str(randint(0,99999)).zfill(5)
    cwd = os.getcwd()
    input_maps = " : ".join([map_stage_in(inp, rwd) for inp in inputs])
    if input_maps:
        input_maps = " -stagein \"" + input_maps + "\""

    output_maps = " : ".join([map_stage_out(outp) for outp in outputs])
    if output_maps:
        output_maps = " -stageout \"" + output_maps + " \""

    std = ""
    if redirected:
        std = std + " -redirected "
    if stdout is not None:
        std = std + " -stdout \"{}\"".format(stdout)
    if stderr is not None:
        std = std + " -stderr \"{}\"".format(stderr)

    return "cog-job-submit -provider \"coaster-persistent\" -attributes \"maxWallTime=240:00:00\" {} -service-contact \"{}\"{}{} -directory \"{}\" /bin/bash -c \"mkdir -p {}; cd {}; {}\"".format(
        std, service_url, input_maps, output_maps, rwd, rwd, rwd, cmd)


def Popen_cjs_cmd(cjs_cmd, pool_name = None):
    # To track submitted cjs commands by pool name
    if pool_name is not None:
        _, cjs_fname = tempfile.mkstemp(prefix = pool_name + "-")
        #cjs_cmd = "sleep 1" # For debugging
        cjs_cmd = "{}; rm {}".format(cjs_cmd, cjs_fname)
    subprocess.Popen(cjs_cmd, shell = True)


def count_cjs_by_pool(pool_names):
    cjs_by_pool = dict.fromkeys(pool_names, 0)
    for pname in pool_names:
        cjs_by_pool[pname] = len(glob.glob('/tmp/' + pname + "-**"))
    return cjs_by_pool


if __name__ == "__main__":
    cmd = "/bin/bash ${exec_work_dir}/wti.sh ${GT_USER} ${sched_ip}"
    service_url = "http://beta.parallel.works:9001"
    inputs = ["pools_info.json", "read_pool_info.py"]
    cjs_cmd = get_cjs_cmd(cmd, service_url, inputs = [], outputs = [], rwd = "${exec_work_dir}")
    print(cjs_cmd)
