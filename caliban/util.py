"""
Utilities for our job runner.
"""
import argparse
import getpass
import io
import itertools as it
import os
import platform
import re
import shutil
import subprocess
import sys
from enum import Enum
from typing import (Any, Callable, Dict, Iterable, List, NamedTuple, Optional,
                    Set, Tuple)

from absl import flags


class Package(NamedTuple):
  package_path: str
  main_module: str


def current_user() -> str:
  return getpass.getuser()


def is_mac() -> bool:
  """Returns True if the current code is executing on a Mac, False otherwise.

  """
  return platform.system() == "Darwin"


def is_linux() -> bool:
  """Returns True if the current code is executing on a Linux system, False
  otherwise.

  """
  return platform.system() == "Darwin"


def enum_vals(enum: Enum) -> List[str]:
  """Returns the list of all values for a specific enum."""
  return [v.value for v in enum]


def dict_product(m: Dict[Any, Iterable[Any]]) -> Iterable[Dict[Any, Any]]:
  """Returns a dictionary generated by taking the cartesian product of each
  list-typed value iterable with all others.

  The iterable of dictionaries returned represents every combination of values.

  If any value is NOT a list it will be treated as a singleton list.

  """

  def wrap_v(v):
    return v if isinstance(v, list) else [v]

  cleaned = {k: wrap_v(v) for k, v in m.items()}

  ks = cleaned.keys()
  vs = cleaned.values()
  return (dict(zip(ks, x)) for x in it.product(*vs))


def compose(l, r):
  """Returns a function that's the composition of the two supplied functions.

  """

  def inner(*args, **kwargs):
    return l(r(*args, **kwargs))

  return inner


def flipm(table: Dict[Any, Dict[Any, Any]]) -> Dict[Any, Dict[Any, Any]]:
  """Handles shuffles for a particular kind of table."""
  ret = {}
  for k, m in table.items():
    for k2, v in m.items():
      ret.setdefault(k2, {})[k] = v

  return ret


def invertm(table: Dict[Any, Iterable[Any]]) -> Dict[Any, Set[Any]]:
  """Handles shuffles for a particular kind of table."""
  ret = {}
  for k, vs in table.items():
    for v in vs:
      ret.setdefault(v, set()).add(k)

  return ret


def reorderm(table: Dict[Any, Dict[Any, Iterable[Any]]],
             order: Tuple[int, int, int]) -> Dict[Any, Dict[Any, Set[Any]]]:
  """Handles shuffles for a particular kind of table."""
  ret = {}
  for k, m in table.items():
    for k2, vs in m.items():
      for v in vs:
        fields = [k, k2, v]
        innerm = ret.setdefault(fields[order[0]], {})
        acc = innerm.setdefault(fields[order[1]], set())
        acc.add(fields[order[2]])

  return ret


def merge(l: Dict[Any, Any], r: Dict[Any, Any]) -> Dict[Any, Any]:
  """Returns a new dictionary by merging the two supplied dictionaries."""
  ret = l.copy()
  ret.update(r)
  return ret


def dict_by(keys: Set[str], f: Callable[[str], Any]) -> Dict[str, Any]:
  """Returns a dictionary with keys equal to the supplied keyset. Each value is
  the result of applying f to a key in keys.

  """
  return {k: f(k) for k in keys}


def expand_args(items: Dict[str, str]) -> List[str]:
  """Converts the input map into a sequence of k, v pair strings. A None value is
  interpreted to mean that the key is a solo flag; it's evicted from the
  output.

  """
  pairs = [[k, v] if v is not None else [k] for k, v in items.items()]
  return list(it.chain.from_iterable(pairs))


def parse_flags_with_usage(args, known_only=False):
  """Tries to parse the flags, print usage, and exit if unparseable.
  Args:
    args: [str], a non-empty list of the command line arguments including
        program name.
  Returns:
    [str], a non-empty list of remaining command line arguments after parsing
    flags, including program name.
  """
  try:
    return flags.FLAGS(args, known_only=known_only)
  except flags.Error as error:
    sys.stderr.write('FATAL Flags parsing error: %s\n' % error)
    sys.stderr.write('Pass --helpshort or --helpfull to see help on flags.\n')
    sys.exit(1)


class TempCopy(object):
  """Inside its scope, this class:

  - generates a temporary file at tmp_name containing a copy of the file at
    original_path, and
  - deletes the new file at tmp_name when the scope exits.

  The temporary file will live inside the current directory where python's
  being executed; it's a hidden file, but it will be live for the duration of
  TempCopy's scope.

  We did NOT use a tmp directory here because the changing UUID name
  invalidates the docker image each time a new temp path / directory is
  generated.

  """

  def __init__(self, original_path, tmp_name=None):
    if tmp_name is None:
      self.tmp_path = ".caliban_tmp_dev_key.json"

    # handle tilde!
    self.original_path = os.path.expanduser(original_path)
    self.relative_path = None
    self.full_path = None

  def __enter__(self):
    if self.original_path is None:
      return None

    current_dir = os.getcwd()
    self.path = os.path.join(current_dir, self.tmp_path)
    shutil.copy2(self.original_path, self.path)
    return self.tmp_path

  def __exit__(self, exc_type, exc_val, exc_tb):
    if self.path is not None:
      os.remove(self.path)
      self.path = None


def capture_stdout(cmd: List[str], input_str: str) -> str:
  """Executes the supplied command with the supplied string of std input, then
  streams the output to stdout, and returns it as a string.
  """
  buf = io.StringIO()
  with subprocess.Popen(cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        bufsize=1,
                        encoding="utf-8") as p:
    p.stdin.write(input_str)
    p.stdin.close()
    for line in p.stdout:
      print(line, end='')
      buf.write(line)

  return buf.getvalue()


def path_to_module(path_str: str) -> str:
  return path_str.replace(".py", "").replace("/", ".")


def module_to_path(module_name: str) -> str:
  return path_to_module(module_name).replace(".", "/") + ".py"


def generate_package(path: str) -> Package:
  """Takes in a string and generates a package instance that we can use for
  imports.

  """
  module = path_to_module(path)
  items = module.split(".")
  root = "." if len(items) == 1 else items[0]
  return Package(root, module)


def validated_package(path: str) -> Package:
  """similar to generate_package but runs argparse validation on packages that
  don't actually exist in the filesystem.

  """
  p = generate_package(path)

  if not os.path.isdir(p.package_path):
    raise argparse.ArgumentTypeError(
        f"""Directory '{p.package_path}' doesn't exist in directory. Modules must be
nested in a folder that exists in the current directory.""")

  filename = module_to_path(p.main_module)
  if not os.path.isfile(os.path.join(os.getcwd(), filename)):
    raise argparse.ArgumentTypeError(
        f"""File '{filename}' doesn't exist locally; modules must live inside the
current directory.""")

  return p


def parse_kv_pair(s: str) -> Tuple[str, str]:
  """
    Parse a key, value pair, separated by '='

    On the command line (argparse) a declaration will typically look like:
        foo=hello
    or
        foo="hello world"
    """
  items = s.split('=')
  k = items[0].strip()  # Remove whitespace around keys

  if len(items) <= 1:
    raise argparse.ArgumentTypeError(
        f"Couldn't parse label '{s}' into k=v format.")

  v = '='.join(items[1:])
  return (k, v)


def _is_key(k: Optional[str]) -> bool:
  """Returns True if the argument is a valid argparse optional arg input, False
  otherwise.

  Strings that start with - or -- are considered valid for now.

  """
  return k is not None and len(k) > 0 and k[0] == "-"


def _truncate(s: str, max_length: int) -> str:
  """Returns the input string s truncated to be at most max_length characters
  long.

  """
  return s if len(s) <= max_length else s[0:max_length]


def _clean_label(s: Optional[str], is_key: bool) -> str:
  """Processes the string into the sanitized format required by AI platform
  labels.

  https://cloud.google.com/ml-engine/docs/resource-labels

  """
  if s is None:
    return ""

  # lowercase, letters, - and _ are valid, so strip the leading dashes, make
  # everything lowercase and then kill any remaining unallowed characters.
  cleaned = re.sub(r'[^a-z0-9_-]', '', s.lstrip("-").lower())

  # Keys must start with a letter. If is_key is set and the cleaned version
  # starts with something else, append `k`.
  if is_key and cleaned != "" and not cleaned[0].isalpha():
    cleaned = "k" + cleaned

  # key and value for labels can be at most 63 characters long.
  return _truncate(cleaned, 63)


def key_label(k: Optional[str]) -> str:
  """converts the argument into a valid label, suitable for submission as a label
  key to Cloud.

  """
  return _clean_label(k, True)


def value_label(v: Optional[str]) -> str:
  """converts the argument into a valid label, suitable for submission as a label
  value to Cloud.

  """
  return _clean_label(v, False)


def n_chunks(items: List[Any], n_groups: int) -> List[List[Any]]:
  """Returns a list of `n_groups` slices of the original list, guaranteed to
  contain all of the original items.

  """
  return [items[i::n_groups] for i in range(n_groups)]


def chunks_below_limit(items: List[Any], limit: int) -> List[List[Any]]:
  """Breaks the input list into a series of chunks guaranteed to be less than"""
  quot, _ = divmod(len(items), limit)
  return n_chunks(items, quot + 1)


def partition(seq: List[str], n: int) -> List[List[str]]:
  """Generate groups of n items from seq by scanning across the sequence and
  taking chunks of n, offset by 1.

  """
  for i in range(0, max(1, len(seq) - n + 1), 1):
    yield seq[i:i + n]


def script_args_to_labels(script_args: Optional[List[str]]) -> Dict[str, str]:
  """Converts the arguments supplied to our scripts into a dictionary usable as
  labels valid for Cloud submission.

  """
  ret = {}
  if script_args is None or len(script_args) == 0:
    return ret

  def process_pair(k, v):
    if _is_key(k):
      clean_k = key_label(k)
      if clean_k != "":
        ret[clean_k] = "" if _is_key(v) else value_label(v)

  for k, v in partition(script_args, 2):
    process_pair(k, v)

  # Handle the case where the final argument in the list is a boolean flag.
  # This won't get picked up by partition.
  if len(script_args) > 1:
    process_pair(script_args[-1], None)

  return ret


def sanitize_labels(pairs: List[Tuple[str, str]]) -> Dict[str, str]:
  """Turns a list of unsanitized key-value pairs (represented by a tuple) into a
  dictionary suitable to submit to Cloud as a label dict.

  """
  return {key_label(k): value_label(v) for (k, v) in pairs if key_label(k)}


def validated_directory(path: str) -> str:
  """This validates that the supplied directory exists locally.

  """
  if not os.path.isdir(path):
    raise argparse.ArgumentTypeError(
        f"""Directory '{path}' doesn't exist in this directory. Check yourself!"""
    )
  return path


def validated_file(path: str) -> str:
  """This validates that the supplied directory exists locally.

  """
  if not os.path.isfile(path):
    raise argparse.ArgumentTypeError(
        f"""File '{path}' isn't a valid file on your system. Try again!""")
  return path
