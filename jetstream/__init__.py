import os
from pkg_resources import get_distribution, resource_filename

__version__ = get_distribution('jetstream').version

site_template_path = resource_filename('jetstream', 'built_in_templates')
task_id_template = 'js{}'
project_index = 'jetstream'
project_config = 'config'
project_temp = 'temp'
project_logs = 'logs'
project_manifest = os.path.join(project_index, 'manifest')
project_workflow = os.path.join(project_index, 'workflow')
project_history = os.path.join(project_index, 'history')

# This prevents numpy from starting a million threads when imported. The
# graph library, networkx, uses scipy/numpy. TODO switch to another graph lib
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'

from jetstream import utils, legacy


data_loaders = {
    '.txt': utils.table_to_records,
    '.csv': utils.table_to_records,
    '.mer': utils.table_to_records,
    '.tsv': utils.table_to_records,
    '.json': utils.json_load,
    '.yaml': utils.yaml_load,
    '.yml': utils.yaml_load,
    '.config': legacy.config.load,
}


from jetstream import legacy, templates, runners, projects, workflows
from jetstream.workflows import Workflow
from jetstream.projects import Project


env = templates.load_environment()


def config_environment(*args, **kwargs):
    global env
    env = templates.load_environment(*args, **kwargs)
