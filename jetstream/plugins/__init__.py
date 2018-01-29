""" Establish a plugin registry to allow a variety of plugin flavors to be used
in workflows.

A uniform description of the plugins themselves needs to be recorded somewhere.
This could be generated by the developer each time a plugin is added, a
corresponding record is added to the table. But, it would be better if these
records were generated from the codebase itself.

For python-package plugins it's easy, just define a set of values that need to be
included in __init__. For script plugins it's much harder because we don't read
them at runtime, they're just executed. We could require a manifest.json be
included along with each script? Or this utility pulls plugins from a central
 location that also warehouses the manifests?


Uses some advanced glob for stageIn/stageOut

Plugin id format:
    "{}/{}:{}".format(plugin, path, revision (optional))

The tools here can be combined for some pretty cool results. Here is a loop that
prints every version of a plugin component available in the archive:

```python
for r in plugins.list_revisions('pegasusPipe/jobScripts/pegasus_firstStrandedSalmon.sh'):
     p = plugins.get_plugin('pegasusPipe/jobScripts/pegasus_firstStrandedSalmon.sh'+ ':'+r['id'])
     print('pegasusPipe/jobScripts/pegasus_firstStrandedSalmon.sh'+ ':'+r['id'])
     print(p.decode())
```

"""
import os
import shutil
import subprocess
import tempfile
import pkg_resources
import glob
import logging
import re

from jetstream import utils

plugin_dir = pkg_resources.resource_filename('jetstream', 'plugins/')

log = logging.getLogger(__name__)


def _clone(repo='https://github.com/tgen/pegasusPipe.git'):
    # TODO Validate that the repo we want is actually a jetstream plugin repo
    # not sure yet about the best place to do this. Jetstream plugin repo is
    # a collection of yaml files that describe plugins, we don't want to try
    # loading files that are not plugins etc..
    log.critical('Cloning {} into {}'.format(repo, plugin_dir))
    subprocess.run(
        ['git', 'clone', repo],
        cwd=plugin_dir
    )


def _remove(plugin):
    """ Careful, this is essentially a shortcut to rm -rf """
    shutil.rmtree(os.path.join(plugin_dir, plugin))


def _get_path(plugin, path, revision=None):
    """ Retrieve a path from plugin. Returns the path as bytes."""
    if revision is None:
        revision = 'HEAD'

    # This function translates its parameters into a 'git show' command
    # to allow for archival access to old scripts. The current version
    # of the path will be used if revision is None. This will throw an
    # error if the path does not exist

    identifier = '{}:{}'.format(revision, path)
    data = subprocess.check_output(
        ['git', 'show', identifier],
        cwd=os.path.join(plugin_dir, plugin)
    )

    return data


def _get_path_revisions(plugin, path):
    """ Returns a list of all the logs for a particular path.
   It may be useful at some point for discovering versions available.
   http://blog.lost-theory.org/post/how-to-parse-git-log-output/
   """
    commit_fields = ['id', 'author_name', 'author_email', 'date', 'message']
    log_fields = ['%H', '%an', '%ae', '%ad', '%s']
    git_log_format = '%x1f'.join(log_fields) + '%x1e'
    format_flag = '--format={}'.format(git_log_format)

    data = subprocess.check_output(
        ['git', 'log', format_flag, '--', path],
        cwd=os.path.join(plugin_dir, plugin)
    ).decode()

    git_log = data.strip('\n\x1e').strip().split('\x1e')
    git_log = [row.strip().split('\x1f') for row in git_log]
    git_log = [dict(zip(commit_fields, row)) for row in git_log]

    return git_log


def _parse_plugin_id(string):
    """ Resolves a plugin id string, returns a dictionary of properties """
    rx = r'(?P<plugin>[^\/]*)\/(?P<path>[^:]*):?(?P<revision>(?<=:)[0-9a-f]{5,40})?$'
    plugin_id_pattern = re.compile(rx)

    match = plugin_id_pattern.match(string)
    if match is None:
        raise ValueError('Invalid plugin id: {}'.format(string))
    else:
        return match.groupdict()


def list():
    """ List all plugin paths available """
    # TODO oh god this is ugly,,
    all = glob.glob(plugin_dir + '/**', recursive=True)
    all = [p for p in all if os.path.isfile(p)]
    all = [utils.remove_prefix(p, plugin_dir) for p in all]
    all = [p for p in all if p and not p.startswith(('_', 'README'))]
    return all


def list_revisions(plugin_id):
    """ Given a plugin id, returns a list of all revisions """
    p = _parse_plugin_id(plugin_id)
    revs = _get_path_revisions(p['plugin'], p['path'])
    return revs


def revision_freeze(plugin_id):
    """ Given plugin id, returns the lastest version as a freeze string """
    p =  _parse_plugin_id(plugin_id)
    revs = _get_path_revisions(p['plugin'], p['path'])
    latest_id = revs[0]['id']
    freeze = '{}/{}:{}'.format(p['plugin'], p['path'], latest_id)
    return freeze


def get_plugin(plugin_id):
    """ Given plugin_id Returns the plugin path. Freeze strings are allowed
    here. """
    p = _parse_plugin_id(plugin_id)
    plugin_data = _get_path(p.get('plugin'), p.get('path'), p.get('revision'))

    # TODO this should return a plugin object, need to parse yaml
    # but right now the plugin library is just a placeholder so
    # the scripts are not yaml

    # t = tempfile.NamedTemporaryFile()

    #plugin_obj = utils.load_yaml()

    # with open(t, 'w') as fp:
    #     fp.write(plugin_obj['script'])
    #
    # plugin_obj['_script_temp_obj'] = t
    # plugin_obj['_script_path'] = t.name
    return plugin_data


def is_available(plugin_id):
    try:
        _parse_plugin_id(plugin_id)
        return True
    except (ValueError, ChildProcessError):
        return False
