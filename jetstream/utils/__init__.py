import csv
import fnmatch
import gzip
import json
import logging
import os
import sys
from datetime import datetime
from getpass import getuser
from socket import gethostname
from uuid import getnode

from pkg_resources import get_distribution
from ruamel import yaml

from jetstream.utils import formats
from jetstream.utils.batch_schedulers import slurm
from jetstream.utils.formats.refdict import parse_refdict_line


def read_group(*, ID=None, CN=None, DS=None, DT=None, FO=None, KS=None,
               LB=None, PG=None, PI=None, PL=None, PM=None, PU=None,
               SM=None, strict=True, **unknown):
    """Returns a SAM group header line. This function takes a set of keyword
    arguments that are known tags listed in the SAM specification:

        https://samtools.github.io/hts-specs/

    Unknown tags will raise a TypeError unless 'strict' is False

    :param strict: Raise error for unknown read group tags
    :return: Read group string
    """
    if unknown and strict:
        raise TypeError('Unknown read group tags: {}'.format(unknown))

    fields = locals()
    col_order = ('ID', 'CN', 'DS', 'DT', 'FO', 'KS', 'LB', 'PG', 'PI', 'PL',
                 'PM', 'PU', 'SM')

    final = ['@RG']
    for field in col_order:
        value = fields.get(field)
        if value is not None:
            final.append('{}:{}'.format(field, value))

    return '\t'.join(final)

# TODO:
# def cna_index_template(refdict):
#     with open(refdict, 'r') as fp:
#         lines = fp.readlines()
#
#     seqs_to_print = {i: None for i in range(1, 25)}
#
#     for l in lines:
#         groups = parse_refdict_line(l)
#
#         if groups is None:
#             continue
#
#     lines = [parse_refdict_line(l) for l in lines]



log = logging.getLogger(__name__)

TEST_RECORDS = [
    {
        'test': 'whitespace',
        'data': ' \t\n\n\x0b\x0c'
    },
    {
        'test': 'punctuation',
        'data': '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'
    },
    {
        'test': 'printable',
        'data': '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
                '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~ \t\n\n\x0b\x0c'
    },
    {
        'test': 'digits',
        'data': '0123456789'
    },
    {
        'test': 'ascii_letters',
        'data': 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
    }
]

class Source(str):
    """String subclass that includes a "line_numbers" property for tracking
    the source code line numbers after lines are split up.

    I considered making this an object composed of a string and line number:

    ```python
    class Source(object):
        def __init__(self, line_number, data):
          self.line_number = line_number
          self.data = data

    line = Source(0, 'Hello World')
    ```

    But, I think it actually complicates most use cases. For example, if the
    source lines were stored in a list, we might want to count a pattern. This
    is easy with a list of strings:

    ```python
        res = mylist.count('pattern')
    ```

    With a custom class you would be forced to do something like:

    ```python
         res = Sum([line for line in lines if line.data == 'pattern'])
    ```

    I think this string subclass inheritance pattern is more difficult to
    explain upfront, but it's much is easier to work with downstream. This class
    behaves exactly like a string except in one case: str.splitlines() which
    generates a list of Source objects instead of strings. """
    def __new__(cls, data='', line_number=None):
        line = super(Source, cls).__new__(cls, data)
        line.line_number = line_number
        return line

    def splitlines(self, *args, **kwargs):
        lines = super(Source, self).splitlines(*args, **kwargs)
        lines = [Source(data, line_number=i) for i, data in enumerate(lines)]
        return lines

    def print_ln(self):
        return '{}: {}'.format(self.line_number, self)


def read_lines_allow_gzip(path):
    """Reads line-separated text files, handles gzipped files and recognizes
    universal newlines. This can cause bytes to be lost when reading from a
    pipe. """
    if is_gzip(path):
        with gzip.open(path, 'rb') as fp:
            data = fp.read().decode('utf-8')
    else:
        with open(path, 'r') as fp:
            data = fp.read()
    lines = data.splitlines()
    return lines

def is_gzip(path, magic_number=b'\x1f\x8b'):
    """ Returns True if the path is gzipped """
    if os.path.exists(path) and not os.path.isfile(path):
        raise OSError("This should only be used with regular files because"
                      "otherwise it will lose some data.")

    with open(path, 'rb') as fp:
        if fp.read(2) == magic_number:
            return True
        else:
            return False


def remove_prefix(string, prefix):
    if string.startswith(prefix):
        return string[len(prefix):]
    else:
        return string


def json_load(path):
    with open(path, 'r') as fp:
        return json.load(fp)


def json_loads(data):
    return json.loads(data)


# TODO Handle multi-document yaml files gracefully
def yaml_load(path):
    with open(path, 'r') as fp:
        return yaml.safe_load(fp)


def yaml_loads(data):
    return yaml.safe_load(data)


def yaml_dump(obj, path):
    with open(path, 'w') as fp:
        return yaml.dump(obj, stream=fp, default_flow_style=False)


def yaml_dumps(obj):
    return yaml.dump(obj, default_flow_style=False)


def filter_documents(docs, criteria):
    """Given a list of mapping objects (docs) and a criteria mapping,
    this function returns a list of objects that match filter
    criteria."""
    matches = list()
    for i in docs:
        for k, v in criteria.items():
            if k not in i:
                log.debug('Dropping "{}" due to "{}" not in doc'.format(i, k))
                break
            if i[k] != v:
                log.debug('Dropping "{}" due to "{}" not == "{}"'.format(i, k, v))
                break
        else:
            matches.append(i)
    return matches


def table_to_records(path):
    """Attempts to load a table, in any format, as a set of records"""
    r = list()
    with open(path, 'r') as fp:
        dialect = csv.Sniffer().sniff(fp.readline())
        log.debug('Sniffed delimiter "{}" for "{}"'.format(
            dialect.delimiter, path))
        fp.seek(0)
        reader = csv.DictReader(fp, delimiter=dialect.delimiter)
        for row in reader:
            r.append(dict(row))
    return r


def write_test_data(path, dialect='unix'):
    """Writes test data out to a file. """
    with open(path, 'w') as fp:
        w = csv.DictWriter(fp, fieldnames=['test', 'data'], dialect=dialect)
        w.writeheader()
        for case in TEST_RECORDS:
            w.writerow(case)
    return path


def fingerprint(to_json=False):
    """Gather system info for recording changes made to projects."""
    fp = {
        'datetime': str(datetime.now()),
        'user': getuser(),
        'version': str(get_distribution(__package__)),
        'sys.version': sys.version,
        'sys.platform': sys.platform,
        'sys.mac': hex(getnode()).upper(),
        'pid': os.getpid(),
        'args': sys.argv,
        'hostname': gethostname(),
        'pwd': os.getcwd()
    }

    if to_json:
        return json.dumps(fp)
    else:
        return fp


def find(path, name=None):
    """Sorta behaves like bash find"""
    for dirname, subdirs, files in os.walk(path):
        for f in files:
            if name is None:
                yield os.path.join(dirname, f)
            elif fnmatch.fnmatch(f, name):
                yield os.path.join(dirname, f)