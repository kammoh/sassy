# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import copy
from datetime import datetime
import json
import os
import re
from pathlib import Path
import subprocess
# from contextlib import contextmanager
import time
from types import SimpleNamespace
from jinja2 import Environment, PackageLoader, StrictUndefined
import logging
from jinja2.loaders import ChoiceLoader
from progress import SHOW_CURSOR
from progress.spinner import Spinner as Spinner
import colored
import hashlib
from typing import Mapping, Union, Dict, List

from .settings import Settings
from ..utils import camelcase_to_snakecase, try_convert
from ..debug import DebugLevel

logger = logging.getLogger()

JsonType = Union[str, int, float, bool, List['JsonType'], 'JsonTree']
JsonTree = Dict[str, JsonType]
StrTreeType = Union[str, List['StrTreeType'], 'StrTree']
StrTree = Dict[str, StrTreeType]


class FlowFatalException(Exception):
    """Fatal error"""
    pass


class NonZeroExit(Exception):
    """Process exited with non-zero return"""
    pass


def final_kill(proc):
    try:
        proc.terminate()
        proc.wait()
        proc.kill()
        proc.wait()
    except:
        pass

# @contextmanager
# def process(*args, **kwargs):
#     proc = subprocess.Popen(*args, **kwargs)
#     try:
#         yield proc
#     finally:
#         final_kill(proc)


def my_print(*args, **kwargs):
    print(*args, **kwargs)


# similar to str.removesuffix in Python 3.9+
def removesuffix(s: str, suffix: str) -> str:
    return s[:-len(suffix)] if suffix and s.endswith(suffix) else s

def removeprefix(s: str, suffix: str) -> str:
    return s[len(suffix):] if suffix and s.startswith(suffix) else s


class Flow():
    """ A flow may run one or more tools and is associated with a single set of settings and a single design. """

    required_settings = {}
    default_settings = {}
    reports_subdir_name = 'reports'
    timeout = 3600 * 2  # in seconds
    name = None

    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):
        return {}

    def __init_subclass__(cls) -> None:
        cls_name = camelcase_to_snakecase(cls.__name__)
        mod_name = cls.__module__
        if mod_name and not mod_name.startswith('xeda.flows.'):
            mod_name = removeprefix(mod_name, "xeda.plugins.")
            m = mod_name.split('.')  # FIXME FIXME FIXME!!!
            cls_name =  m[0] + "." + cls_name
        cls.name = cls_name

    def __init__(self, settings: Settings, args: SimpleNamespace, completed_dependencies: List['Flow']):

        self.args = args
        self.xedahash = None
        self.run_path = None
        self.xeda_run_dir = Path(args.xeda_run_dir)
        self.results_dir = self.xeda_run_dir / 'Results' / self.name
        self.results_dir.mkdir(exist_ok=True, parents=True)
        self.flow_run_dir = None
        self.reports_dir = None
        self.init_time = 0

        self.settings = settings
        self.nthreads = int(settings.flow.get('nthreads', 1))

        self.results = dict()
        self.results['success'] = False

        self.jinja_env = Environment(
            loader=ChoiceLoader(
                [
                    PackageLoader(self.__module__, 'templates'),
                    PackageLoader(self.__class__.__module__, 'templates'),
                ] +
                [
                    PackageLoader(clz.__module__, 'templates') for clz in self.__class__.__bases__
                ]
            ),
            autoescape=False,
            undefined=StrictUndefined
        )

        self.no_console = args.debug >= DebugLevel.LOW
        self.post_run_hooks = []
        self.post_results_hooks = []

        self.completed_dependencies = completed_dependencies

    def run_flow(self):
        self.prepare()

        if not self.flow_run_dir.exists():
            self.flow_run_dir.mkdir(parents=True)
        else:
            logger.warning(
                f'Using existing run directory: {self.flow_run_dir}')

        assert self.flow_run_dir.is_dir()

        self.check_settings()
        self.dump_settings()

        self.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.init_time = time.monotonic()
        self.run()

    def gen_xeda_hash(self):
        def semantic_hash(data: JsonTree, hasher=hashlib.sha1) -> str:
            def get_digest(b: bytes):
                return hasher(b).hexdigest()[:32]

            # data: JsonType, not adding type as Pylance does not seem to like recursive types :/
            def sorted_dict_str(data) -> StrTreeType:
                if isinstance(data, Mapping):
                    return {k: sorted_dict_str(data[k]) for k in sorted(data.keys())}
                elif isinstance(data, list):
                    return [sorted_dict_str(val) for val in data]
                elif hasattr(data, '__dict__'):
                    return sorted_dict_str(data.__dict__)
                else:
                    return str(data)

            return get_digest(bytes(repr(sorted_dict_str(data)), 'UTF-8'))

        try:
            return semantic_hash(self.settings)
        except FileNotFoundError as e:
            self.fatal(f"Semantic hash failed: {e} ")

    def check_settings(self):
        for req_key, req_type in self.required_settings.items():
            if req_key not in self.settings.flow:
                self.fatal(f'{req_key} is required to be set for {self.name}')
            # else:
                # val = self.settings.flow[req_key]
                # if (typing.get_origin(req_type) is Union and not isinstance(val, typing.get_args(req_type))) or not isinstance(val,  req_type):
                #     self.fatal(f'{req_key} should have type `{req_type.__name__}` for {self.name}')

    def prepare(self):
        design_settings = self.settings.design

        for section in ['rtl', 'tb']:
            section_settings = design_settings.get(section)
            if section_settings:
                section_settings['sources'] = [
                    DesignSource(src) if isinstance(src, str) else src for src in section_settings.get('sources', [])
                ]

                generics = section_settings.get("generics", {})
                for gen_key, gen_val in generics.items():
                    if isinstance(gen_val, dict) and 'file' in gen_val:
                        path = gen_val['file']
                        logger.debug(
                            f'Generic `{gen_key}` marked with `file` attribute is treated as FileResource({path})')
                        generics[gen_key] = FileResource(path)

        # all design flow-critical settings should be fixed from this point onwards

        self.xedahash = self.gen_xeda_hash()

        self.run_path = Path(self.args.force_run_dir) if self.args.force_run_dir else (
            self.xeda_run_dir / '.xeda_run' / self.xedahash)

        self.run_path = self.run_path.resolve()

        self.flow_run_dir = self.run_path / self.name
        self.reports_dir = self.flow_run_dir / self.reports_subdir_name

    def run(self):
        # Must be implemented
        # extraneous `if` needed for Pylace
        if self:
            raise NotImplementedError

    def parse_reports(self):
        # Do nothing if not implemented
        pass

    def dump_settings(self):
        effective_settings_json = self.flow_run_dir / f'settings.json'
        logger.info(f'dumping effective settings to {effective_settings_json}')
        self.dump_json(self.settings, effective_settings_json)

    def copy_from_template(self, resource_name, **kwargs):
        template = self.jinja_env.get_template(resource_name)
        script_path = self.flow_run_dir / resource_name
        logger.debug(f'generating {script_path.resolve()} from template.')
        rendered_content = template.render(flow=self.settings.flow,
                                           design=self.settings.design,
                                           project=self.settings.project,
                                           nthreads=self.nthreads,
                                           debug=self.args.debug,
                                           reports_dir=self.reports_subdir_name,
                                           **kwargs)
        with open(script_path, 'w') as f:
            f.write(rendered_content)
        return resource_name

    def conv_to_relative_path(self, src):
        path = Path(src).resolve(strict=True)
        return os.path.relpath(path, self.flow_run_dir)

    def fatal(self, msg):
        logger.critical(msg)
        raise FlowFatalException(msg)

    def run_process(self, prog, prog_args, check=True, stdout_logfile=None, initial_step=None, force_echo=False, nolog=False):
        prog_args = [str(a) for a in prog_args]
        if nolog:
            subprocess.check_call([prog] + prog_args, cwd=self.flow_run_dir)
            return
        if not stdout_logfile:
            stdout_logfile = f'{prog}_stdout.log'
        proc = None
        spinner = None
        unicode = True
        verbose = not self.args.quiet and (self.args.verbose or force_echo)
        echo_instructed = False
        stdout_logfile = self.flow_run_dir / stdout_logfile
        start_step_re = re.compile(
            r'^={12}=*\(\s*(?P<step>[^\)]+)\s*\)={12}=*')
        enable_echo_re = re.compile(r'^={12}=*\( \*ENABLE ECHO\* \)={12}=*')
        disable_echo_re = re.compile(r'^={12}=*\( \*DISABLE ECHO\* \)={12}=*')
        error_msg_re = re.compile(r'^\s*error:?\s+', re.IGNORECASE)
        warn_msg_re = re.compile(r'^\s*warning:?\s+', re.IGNORECASE)
        critwarn_msg_re = re.compile(
            r'^\s*critical\s+warning:?\s+', re.IGNORECASE)

        def make_spinner(step):
            if self.no_console:
                return None
            return Spinner('⏳' + step + ' ' if unicode else step + ' ')

        redirect_std = self.args.debug < DebugLevel.HIGH
        with open(stdout_logfile, 'w') as log_file:
            try:
                logger.info(
                    f'Running `{prog} {" ".join(prog_args)}` in {self.flow_run_dir}')
                with subprocess.Popen([prog, *prog_args],
                                      cwd=self.flow_run_dir,
                                      shell=False,
                                      stdout=subprocess.PIPE if redirect_std else None,
                                      bufsize=1,
                                      universal_newlines=True,
                                      encoding='utf-8',
                                      errors='replace'
                                      ) as proc:
                    logger.info(
                        f'Started {proc.args[0]}[{proc.pid}].{(" Standard output is logged to: " + str(stdout_logfile)) if redirect_std else ""}')

                    def end_step():
                        if spinner:
                            if unicode:
                                print('\r✅', end='')
                            spinner.finish()
                    if redirect_std:
                        if initial_step:
                            spinner = make_spinner(initial_step)
                        while True:
                            line = proc.stdout.readline()
                            if not line:
                                end_step()
                                break
                            log_file.write(line)
                            log_file.flush()
                            if verbose or echo_instructed:
                                if disable_echo_re.match(line):
                                    echo_instructed = False
                                else:
                                    print(line, end='')
                            else:
                                if not self.args.quiet:
                                    if error_msg_re.match(line) or critwarn_msg_re.match(line):
                                        if spinner:
                                            print()
                                        logger.error(line)
                                    elif warn_msg_re.match(line):
                                        if spinner:
                                            print()
                                        logger.warning(line)
                                    elif enable_echo_re.match(line):
                                        if not self.args.quiet:
                                            echo_instructed = True
                                        end_step()
                                    else:
                                        match = start_step_re.match(line)
                                        if match:
                                            end_step()
                                            step = match.group('step')
                                            spinner = make_spinner(step)
                                        else:
                                            if spinner:
                                                spinner.next()

            except FileNotFoundError as e:
                self.fatal(
                    f"Cannot execute `{prog}`. Make sure it's properly installed and is in the current PATH")
            except KeyboardInterrupt as e:
                if spinner:
                    print(SHOW_CURSOR)
                final_kill(proc)
            finally:
                final_kill(proc)

        if spinner:
            print(SHOW_CURSOR)

        if proc.returncode != 0:
            m = f'`{proc.args[0]}` exited with returncode {proc.returncode}'
            logger.critical(
                f'{m}. Please check `{stdout_logfile}` for error messages!')
            if check:
                raise NonZeroExit(m)
        else:
            logger.info(
                f'Execution of {prog} in {self.flow_run_dir} completed with returncode {proc.returncode}')

    def parse_report_regex(self, reportfile_path, re_pattern, *other_re_patterns, dotall=True):
        # TODO fix debug and verbosity levels!
        high_debug = self.args.verbose
        if not reportfile_path.exists():
            logger.warning(
                f'File {reportfile_path} does not exist! Most probably the flow run had failed.\n Please check log files in {self.flow_run_dir}'
            )
            return False
        with open(reportfile_path) as rpt_file:
            content = rpt_file.read()

            flags = re.MULTILINE | re.IGNORECASE
            if dotall:
                flags |= re.DOTALL

            def match_pattern(pat):
                match = re.search(pat, content, flags)
                if match is None:
                    return False
                match_dict = match.groupdict()
                for k, v in match_dict.items():
                    self.results[k] = try_convert(v)
                    logger.debug(f'{k}: {self.results[k]}')
                return True

            for pat in [re_pattern, *other_re_patterns]:
                matched = False
                if isinstance(pat, list):
                    if high_debug:
                        logger.debug(f"Matching any of: {pat}")
                    for subpat in pat:
                        matched = match_pattern(subpat)
                        if matched:
                            if high_debug:
                                logger.debug("subpat matched!")
                            break
                else:
                    if high_debug:
                        logger.debug(f"Matching: {pat}")
                    matched = match_pattern(pat)

                if not matched:
                    self.fatal(
                        f"Error parsing report file: {rpt_file.name}\n Pattern not matched: {pat}\n")
        return True

    def print_results(self, results=None):
        if not results:
            results = self.results
            # init to print_results time:
            if results.get('runtime_minutes') is None:
                results['runtime_minutes'] = (
                    time.monotonic() - self.init_time) / 60
        data_width = 32
        name_width = 80 - data_width
        hline = "-"*(name_width + data_width)

        my_print("\n" + hline)
        my_print(f"{'Results':^{name_width + data_width}s}")
        my_print(hline)
        for k, v in results.items():
            if v is not None and not k.startswith('_'):
                if isinstance(v, float):
                    my_print(f'{k:{name_width}}{v:{data_width}.3f}')
                elif isinstance(v, bool):
                    bdisp = (colored.fg(
                        "green") + "✓" if v else colored.fg("red") + "✗") + colored.attr("reset")
                    my_print(f'{k:{name_width}}{bdisp:>{data_width}}')
                elif isinstance(v, int):
                    my_print(f'{k:{name_width}}{v:>{data_width}}')
                elif isinstance(v, list):
                    my_print(
                        f'{k:{name_width}}{" ".join([str(x) for x in v]):<{data_width}}')
                else:
                    my_print(f'{k:{name_width}}{str(v):>{data_width}s}')
        my_print(hline + "\n")

    def dump_json(self, data, path: Path):
        if path.exists():
            modifiedTime = os.path.getmtime(path)
            suffix = datetime.fromtimestamp(
                modifiedTime).strftime("%Y-%m-%d-%H%M%S")
            backup_path = path.with_suffix(f".backup_{suffix}.json")
            logger.warning(
                f"File already exists! Backing-up existing file to {backup_path}")
            os.rename(path, backup_path)

        with open(path, 'w') as outfile:
            json.dump(data, outfile, default=lambda x: x.__dict__ if hasattr(
                x, '__dict__') else str(x), indent=4)

    def dump_results(self):
        path = self.flow_run_dir / f'results.json'
        self.dump_json(self.results, path)
        logger.info(f"Results written to {path}")


class SimFlow(Flow):
    required_settings = {}

    @property
    def sim_sources(self):
        tb_settings = self.settings.design["tb"]
        srcs = self.settings.design["rtl"]['sources']
        for src in tb_settings['sources']:
            if not src in srcs:
                srcs.append(src)
        return srcs

    @property
    def sim_tops(self) -> List[str]:
        """ a view of tb.top that returns a list of primary_unit [secondary_unit] """
        # TODO is there ever a >= ternary_unit? If not switch to primary_unit, secondary_unit instead of the sim_tops list
        tb_settings = self.settings.design["tb"]
        tops = copy.deepcopy(tb_settings['top'])
        if not isinstance(tops, list):
            tops = [tops]
        configuration_specification = tb_settings.get(
            'configuration_specification')
        if configuration_specification:
            tops[0] = configuration_specification
        return tops

    @property
    def tb_top(self) -> str:
        top = self.settings.design["tb"]['top']
        if isinstance(top, list):
            top = top[0]
        return top

    def parse_reports(self):
        self.results['success'] = True

    @property
    def vcd(self) -> str:
        vcd = self.settings.flow.get('vcd')
        if vcd:
            if not isinstance(vcd, str): # e.g. True
                vcd = 'dump.vcd'
            elif not vcd.endswith('.vcd'):
                vcd += '.vcd'
        elif self.args.debug >= DebugLevel.LOW:
            vcd = 'debug_dump.vcd'
        return vcd


class SynthFlow(Flow):
    required_settings = {'clock_period': float}

    # TODO FIXME set in plugin or elsewhere!!!
    default_settings = {'allow_dsps': False, 'allow_brams': False}

    def __init__(self, settings: Settings, args: SimpleNamespace, completed_dependencies: List['Flow']):
        if not isinstance(self, SimFlow):
            settings.design['tb'] = {}
        super().__init__(settings, args, completed_dependencies)


class DseFlow(Flow):
    pass


class FileResource:
    @classmethod
    def is_file_resource(cls, src):
        return isinstance(src, cls) or (isinstance(src, dict) and 'file' in src)

    def __init__(self, path) -> None:
        self.file = None
        self.hash = None

        try:
            # path must be absolute
            self.file = Path(path).resolve(strict=True)
        except Exception as e:
            logger.critical(f"Design source file '{path}' does not exist!")
            raise e

        with open(self.file, 'rb') as f:
            self.hash = hashlib.sha256(f.read()).hexdigest()

    def __eq__(self, other):
        # path is already absolute
        return self.hash == other.hash and self.file.samefile(other.file)

    def __hash__(self):
        # path is already absolute
        return hash(tuple(self.hash, str(self.file)))

    def __str__(self):
        return str(self.file)

    def __repr__(self) -> str:
        return 'FileResource:' + self.__str__()


class DesignSource(FileResource):
    @classmethod
    def is_design_source(cls, src):
        return cls.is_file_resource(src)

    def __init__(self, file: str, type: str = None, standard: str = None, variant: str = None) -> None:

        super().__init__(file)

        def type_from_suffix(file: Path) -> str:
            type_variants_map = {
                ('vhdl', variant): ['vhd', 'vhdl'],
                ('verilog', variant): ['v'],
                ('verilog', 'systemverilog'): ['sv'],
                ('bsv', variant): ['bsv'],
                ('bs', variant): ['bs'],
            }
            for h, suffixes in type_variants_map.items():
                if file.suffix[1:] in suffixes:
                    return h
            return None, None

        self.type, self.variant = (
            type, variant) if type else type_from_suffix(self.file)
        self.standard = standard
