import base64
import importlib
import subprocess  # nosec
import sys
import types
from copy import deepcopy
from pathlib import Path
from time import time
from typing import Callable, List, Optional, Union

import yaml

from devops.lib.log import logger


class Settings:
    """
    Really, settings is a module, but don't tell anyone.
    """

    COMPONENTS: List[str]
    KUBE_CONTEXT: str
    KUBE_NAMESPACE: str
    IMAGE_PULL_SECRETS: Optional[dict]
    REPLICAS: Optional[dict]


def load_env_settings(env: str) -> Settings:
    module = f"envs.{env}.settings"
    if module in sys.modules:
        del sys.modules[module]
    logger.info(f"Loading settings from {module}")
    settings = importlib.import_module(module)

    # Set some defaults for optional values
    settings.IMAGE_PULL_SECRETS = getattr(settings, "IMAGE_PULL_SECRETS", {})
    settings.REPLICAS = getattr(settings, "REPLICAS", {})

    return settings


def list_envs() -> List[str]:
    envs = [
        path.name
        for path in Path("envs").iterdir()
        if path.is_dir()
        and not path.name.startswith("__")
        and not path.name.startswith(".")
    ]

    return envs


def master_key_path(env: str) -> Path:
    return Path("envs") / env / "master.key"


def secrets_pem_path(env: str) -> Path:
    return Path("envs") / env / "secrets.pem"


def run(
    args, cwd=None, check=True, env=None, stream=False, timeout=None, input=None
) -> subprocess.CompletedProcess:
    """
    Run a command

    :param List[str] args:
    :param str cwd:
    :param bool check:
    :param dict env:
    :param bool stream: If the output should be streamed instead of captured
    :param float timeout: Seconds to wait before failing
    :param bytes input: Data to be sent to the child process via stdin
    :raises subprocess.CalledProcessError:
    :raises subprocess.TimeoutExpired:
    :return subprocess.CompletedProcess:
    """
    # Convert Paths to strings
    for index, value in enumerate(args):
        args[index] = str(value)
    logger.info("  " + " ".join(args))

    kwargs = {"cwd": cwd, "check": check, "env": env, "input": input}

    if not stream:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    if timeout:
        kwargs["timeout"] = timeout

    start = time()
    try:
        res = subprocess.run(args, **kwargs)  # nosec
    except subprocess.CalledProcessError as e:
        logger.error("Failed to run " + " ".join(args))
        log_subprocess_output(e, logger.error)
        logger.error(f"  ✘ ... failed in {time() - start:.3f}s")
        raise
    else:
        log_subprocess_output(res, logger.debug)
        logger.info(f"  ✔ ... done in {time() - start:.3f}s")
        return res


def log_subprocess_output(
    res: Union[subprocess.CompletedProcess, subprocess.CalledProcessError],
    log: Callable,
):
    if res.stdout:
        log("  ----- STDOUT -----")
        log(res.stdout.decode("utf-8").strip())
    if res.stderr:
        log("  ----- STDERR -----")
        log(res.stderr.decode("utf-8").strip())
    if res.stdout or res.stderr:
        log("  ------------------")


def label(fn, text: str):
    l = len(text)
    fill = "-" * l

    fn(f"/-{fill}-\\")
    fn(f"| {text} |")
    fn(f"\\-{fill}-/")


def big_label(fn, text: str):
    l = len(text)
    fill = "-" * l
    padd = " " * l

    fn("")
    fn(f"/---{fill}---\\")
    fn(f"|   {padd}   |")
    fn(f"|   {text}   |")
    fn(f"|   {padd}   |")
    fn(f"\\---{fill}---/")
    fn("")


def merge_docs(
    src: List[dict], overrides: List[dict], base_overrides: List[dict]
) -> List[dict]:
    """
    Merges Yaml documents.

    You need to load the source documents using yaml.Loader (src). The overriding
    documents needs to be loaded with both yaml.Loader (overrides) and yaml.BaseLoader
    (base_overrides) for this to work properly.

    :param src: The source documents loaded with yaml.Loader.
    :param overrides: The override documents loaded with yaml.Loader. Contains the
    values with the actual types.
    :param base_overrides: The override documents loaded with yaml.BaseLoader.
    Contains the literal values, e.g. the tilde (~) as a literal, instead of None.
    :return: New list of merged values
    """
    docs = deepcopy(src)

    def _merge_part(doc, overrides, base_overrides, path=""):
        """
        Merge the trees - recursive part of logic
        """

        def _nest(_doc, _overrides, _base_overrides, _path):
            """
            Support nesting even when original doc ran out of matching data
            """
            if _doc is None:
                _doc = type(_overrides)()

            return _merge_part(_doc, _overrides, _base_overrides, _path)

        if type(doc) == dict:
            res = {}
            for key in base_overrides:
                if base_overrides[key] == "~":
                    # Remove these from target
                    pass
                elif overrides[key] == "":
                    # Use original value
                    res[key] = doc[key]
                elif type(overrides[key]) in (str, int, bool, float, complex):
                    # Simply overridden values
                    res[key] = overrides[key]
                elif key not in doc:
                    # Added values
                    res[key] = _nest(
                        None, overrides[key], base_overrides[key], f"{path}.{key}"
                    )
                else:
                    # Nesting
                    res[key] = _nest(
                        doc[key], overrides[key], base_overrides[key], f"{path}.{key}"
                    )

                # Remove all overridden values from source doc so we can later just
                # copy the remaining values over
                if key in doc:
                    del doc[key]

            for key in doc:
                res[key] = doc[key]

            return res
        elif type(doc) == list:
            res = []
            for idx, base_value_override in enumerate(base_overrides):
                value_override = overrides[idx]
                if idx > len(doc) - 1:
                    # Added values
                    if isinstance(base_value_override, types.GeneratorType):
                        res.append(
                            _nest(
                                None,
                                value_override,
                                base_value_override,
                                f"{path}[{idx}]",
                            )
                        )
                    else:
                        res.append(value_override)
                    continue

                value = doc[idx]
                if base_value_override == "~":
                    # Remove these from target
                    continue
                elif base_value_override == "":
                    # Use original value
                    res.append(value)
                elif type(value_override) in (str, int, bool, float, complex):
                    # Simply overridden values
                    res.append(value_override)
                else:
                    res.append(
                        _nest(
                            value, value_override, base_value_override, f"{path}[{idx}]"
                        )
                    )

            if len(doc) > len(overrides):
                for item in doc[len(overrides) :]:
                    res.append(item)

            return res
        else:
            raise NotImplementedError(f"Dunno how to merge {type(doc)}")

    for i, doc in enumerate(docs):
        docs[i] = _merge_part(doc, overrides[i], base_overrides[i])

    return docs


def get_merged_kube_file(path: Path, merge_temp: Path) -> Path:
    """
    Return a path to a fully merged version of the given kube file.

    If the path is not a merge file it's returned as is.
    If it's a merge file a merged copy will be stored in the given temp
    directory and the path to the generated file will be returned.

    :param path: The path to the partial file
    :param merge_temp: A path under which the merged file will be stored
    :return: The path to the fully merged file
    """
    if not (path.parts[0] == "envs" and path.parts[2] == "merges"):
        # No merging needed
        return path

    master_file = Path().joinpath(*path.parts[3:])
    output_file = merge_temp / path

    logger.debug(f"Merging file {path} with {master_file} to {output_file}")

    with master_file.open("r", encoding="utf-8") as f:
        src = list(yaml.load_all(f, yaml.Loader))
    with path.open("r", encoding="utf-8") as f:
        overrides = list(yaml.load_all(f, yaml.Loader))
    # Use the BaseLoader to get literal values, such as tilde (~).
    with path.open("r", encoding="utf-8") as f:
        base_overrides = list(yaml.load_all(f, yaml.BaseLoader))

    merged_docs = merge_docs(src, overrides, base_overrides)

    output_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as f:
        yaml.dump_all(merged_docs, stream=f, Dumper=yaml.Dumper)

    return output_file


def normalize_line_endings(data: str) -> str:
    """
    Normalize line endings of given input to \n, same universal format as used in
    PEP 278 -- Universal Newline Support.

    :param data: A string with Unix, Windows or Mac newlines.
    :return: A string with universal (Unix) newlines.
    """
    unix_nl = "\n"
    windows_nl = "\r\n"
    mac_nl = "\r"

    return data.replace(windows_nl, unix_nl).replace(mac_nl, unix_nl)


def base64decode(data: str) -> str:
    """
    Base64 decode a string.

    :param data: The base64 encoded string.
    :return: The decoded string.
    """
    return base64.b64decode(data.encode()).decode()


def base64encode(data: str) -> str:
    """
    Base64 encode a string.

    :param data: The decoded string.
    :return: The base64 encoded string.
    """
    return base64.b64encode(data.encode()).decode()
