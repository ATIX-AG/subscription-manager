# Read and parse /proc/cpuinfo
#
# Copyright (c) 2015 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

# needs to be able to provide a data object for each cpu blob and
# for the system
#
# needs to be able to aggregate multiple cpu data objects and create
# an exemplar cpuinfo  (ie, if we want to ignore cpus at different
# speeds, this may be where)
#
# needs to work non-root if possible, possibly as a standalone module/cli
#
# Expect the info available in cpuinfo to very across arches, and across
# kernel and cpu versions. Some arches have almost no info. Some have tons.
# Some provide hex values, most decimal.
#
# Expect the field names to change often. Don't expect field names to
# be unique.
#
# Expect some fields to disappear without warning at any oppurtunity
# (This includes changing arch, version, kernel, os vendor. It also includes
#  no reason at all. cpus can disappear. cpus can remove fields. they can
#  reappear).
#
# Expect values of cpuinfo fields to change, somethings constantly. cpu speed
# for example, can actually vary _every_ time it is read.
#
# GOTCHAS: the field names are non consistent across arches, and can conflict
#          semantically.
#
#         surprise, some are not even one key: value per line (see s390x)
#
# context manager?
# class CpuinfoFile()
#     .read()
#  handle io errors
#
# can take file like object or
# class BaseParseCpuInfo()
#
# class FieldNameCanonicalizer()
#  ie, convert 'model name' to model_name
#  and 'proccesor' processor
#  and 'Processor' to... seriously aarch64?
#    ('Processor' and 'processor' fields...)
#
# class CpuInfo() the main interface class
#     arch = None
#     cpuinfo_class = None
#     # avoid, count of cpus/sockets/etc
#
#
# class X86_64():
#
# class S390X():
#   with fun "multiple values per line"
#
# class Aarch64():
#   with hex values and a system stanza
#
# class Ppc64():
#    system stanza and system model
# factory to init proper one based... uname.machine? 'arch' file?

import collections
import itertools
import logging
import os

# mostly populated from the arm CPUID instruction
# http://infocenter.arm.com/help/index.jsp?topic=/com.arm.doc.ddi0432c/Bhccjgga.html
# http://infocenter.arm.com/help/index.jsp?topic=/com.arm.doc.ddi0432c/Chdeaeij.html
#
# aarch "version" info
# CPU implementer : 0x50
# CPU architecture: AArch64
# CPU variant : 0x0
# CPU part    : 0x000
# CPU revision    : 0

# Mostly info from intel CPUID instruction
# http://en.wikipedia.org/wiki/CPUID
#
# intel "version" info
# processor   : 22
# vendor_id   : GenuineIntel
# cpu family  : 6
# model       : 45
# model name  : Intel(R) Xeon(R) CPU E5-2630 0 @ 2.30GHz
# stepping    : 7
# microcode   : 0x710
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Sequence, Tuple

log = logging.getLogger(__name__)


class DefaultCpuFields:
    """Maps generic cpuinfo fields to the corresponding field from ProcessorModel.

    For, a cpu MODEL (a number or string that the cpu vendor assigns to that model of
    cpu, '45' for an intel Xeon for example)
    is in the 'model' field in /proc/cpuinfo, and the
    'model' in the sluggified field in X86_64ProcessorModel. For aarch64,
    the field 'cpu_part' is it's MODEL.
    """

    MODEL_NAME = "model_name"
    MODEL = "model"


class X86_64Fields:
    MODEL_NAME = "model_name"
    MODEL = "model"


class Aarch64Fields:
    MODEL = "cpu_part"
    MODEL_NAME = "model_name"


class Ppc64Fields:
    MODEL = "model"
    MODEL_NAME = "machine"


# represent the data in /proc/cpuinfo, which may include multiple processors
class CpuinfoModel:
    fields_class = DefaultCpuFields

    def __init__(self, cpuinfo_data: Optional[Dict] = None):
        # The contents of /proc/cpuinfo
        self.cpuinfo_data: Optional[Dict] = cpuinfo_data

        # A iterable of CpuInfoModels, one for each processor in cpuinfo
        self.processors: List[CpuinfoModel] = []

        # prologues or footnotes not associated with a particular processor
        self.other: List[str] = []

        # If were going to pretend all the cpus are the same,
        # what do they all have in common.
        self.common: Dict = {}

        # model name    : Intel(R) Core(TM) i5 CPU       M 560  @ 2.67GHz
        self._model_name = None

        # a model number
        # "45" for intel processor example above
        self._model = None

    @property
    def count(self) -> int:
        return len(self.processors)

    @property
    def model_name(self) -> Optional[str]:
        if self._model_name:
            return self._model_name

        if not self.common:
            return None

        return self.common.get(self.fields_class.MODEL_NAME, None)

    @property
    def model(self) -> Optional[str]:
        if self._model:
            return self._model

        if not self.common:
            return None

        return self.common.get(self.fields_class.MODEL, None)

    def __str__(self) -> str:
        lines: List[str] = []
        lines.append("Processor count: %s" % self.count)
        lines.append("model_name: %s" % self.model_name)
        lines.append("")
        for k in sorted(self.common.keys()):
            lines.append("%s: %s" % (k, self.common[k]))
        lines.append("")
        for k, v in self.other:
            lines.append("%s: %s" % (k, v))
        lines.append("")
        return "\n".join(lines)


class Aarch64ProcessorModel(dict):
    """The info corresponding to the info about each aarch64 processor entry in cpuinfo"""

    pass


class X86_64ProcessorModel(dict):
    """The info corresponding to the info about each X86_64 processor entry in cpuinfo"""

    pass


class Ppc64ProcessorModel(dict):
    """The info corresponding to the info about each ppc64 processor entry in cpuinfo"""

    @classmethod
    def from_stanza(cls: type, stanza: Sequence[str]):
        cpu_data: cls = cls()
        cpu_data.update(dict([fact_sluggify_item(item) for item in stanza]))
        return cpu_data


class X86_64CpuinfoModel(CpuinfoModel):
    """The model for all the cpuinfo data for all processors on the machine.

    ie, all the data in /proc/cpuinfo field as opposed to X86_64ProcessModel which
    is the info for 1 processor."""

    fields_class = X86_64Fields


class Ppc64CpuinfoModel(CpuinfoModel):
    fields_class = Ppc64Fields


class Aarch64CpuinfoModel(CpuinfoModel):
    fields_class = Aarch64Fields


def fact_sluggify(key: str) -> str:
    """Encodes an arbitrary string to something that can be used as a fact name.

    ie, 'model_name' instead of 'Model name'
    whitespace -> _
    lowercase
    utf8
    escape quotes

    In theory, any utf8 would work
    """
    # yeah, terrible...
    return key.lower().strip().replace(" ", "_").replace(".", "_")


def fact_sluggify_item(item_tuple: Iterable[str]) -> Tuple[str, str]:
    newkey: str = fact_sluggify(item_tuple[0])
    return (newkey, item_tuple[1])


# FIXME Argument shadows variaable name from outer scope
def split_key_value_generator(file_contents: str, line_splitter: Callable) -> Iterator[List[str]]:
    for line in file_contents.splitlines():
        parts: List[str] = line_splitter(line)
        if parts:
            yield parts


def line_splitter(line: str) -> Optional[List[str]]:
    # cpu family    : 6
    # model name    : Intel(R) Core(TM) i5 CPU       M 560  @ 2.67GHz
    parts: List[str] = line.split(":", 1)
    if parts[0]:
        parts = [part.strip() for part in parts]
        return parts
    return None


def accumulate_fields(fields_accum: Set, fields: Sequence) -> Set:
    for field in fields:
        fields_accum.add(field)
    return fields_accum


def find_shared_key_value_pairs(
    all_fields: Iterable[str], processors: Sequence[Dict[str, Set]]
) -> Dict[str, Set]:
    # smashem, last one wins
    smashed: Dict[str, Set] = collections.defaultdict(set)

    # build a dict of fieldname -> list of all the different values
    # so we can dump the variant ones.
    for field in all_fields:
        for k, v in [(field, processor.get(field)) for processor in processors]:
            if v is None:
                continue
            smashed[k].add(v)

    # remove fields that can't be smashed to one value
    common_cpu_info = dict([(x, smashed[x].pop()) for x in smashed if len(smashed[x]) == 1])
    return common_cpu_info


def split_kv_list_by_field(kv_list: Sequence[Tuple[str, Any]], field: str) -> Iterator[List[Tuple[str, str]]]:
    """Split the iterable kv_list into chunks by field.

    For a list with repeating stanzas in it, this will
    return a generate that will return each chunk.

    For something like /proc/cpuinfo, called with
    field 'processor', each stanza is a different cpu.
    """
    current_stanza: List[Tuple[str, Any]] = None
    for key, value in kv_list:
        if key == field:
            if current_stanza:
                yield current_stanza
            current_stanza = [(key, value)]
            continue

        # if we have garbage in and no start to processor info
        if current_stanza:
            current_stanza.append((key, value))

    # end of kv_list
    if current_stanza:
        yield current_stanza


"""
Processor   : AArch64 Processor rev 0 (aarch64)
processor   : 0
processor   : 1
processor   : 2
processor   : 3
processor   : 4
processor   : 5
processor   : 6
processor   : 7
Features    : fp asimd evtstrm
CPU implementer : 0x50
CPU architecture: AArch64
CPU variant : 0x0
CPU part    : 0x000
CPU revision    : 0

Hardware    : APM X-Gene Mustang board
"""


class BaseCpuInfo:
    @classmethod
    def from_proc_cpuinfo_string(cls: type, proc_cpuinfo_string: str):
        """Return a BaseCpuInfo subclass based on proc_cpuinfo_string.

        proc_cpuinfo_string is the string resulting from reading
        the entire contents of /proc/cpuinfo."""
        cpu_info: BaseCpuInfo = cls()
        cpu_info._parse(proc_cpuinfo_string)

        return cpu_info

    # FIXME Implement _parse function doing `pass` or raising NotImplementedError


class Aarch64CpuInfo(BaseCpuInfo):
    def __init__(self):
        self.cpu_info = Aarch64CpuinfoModel()

    def _parse(self, cpuinfo_data: str) -> None:
        raw_kv_iter: Iterator = split_key_value_generator(cpuinfo_data, line_splitter)

        # Yes, there is a 'Processor' field and multiple lower case 'processor'
        # fields.
        kv_iter: Iterator = (self._capital_processor_to_model_name(item) for item in raw_kv_iter)

        slugged_kv_list: List[Tuple[str, str]] = [fact_sluggify_item(item) for item in kv_iter]

        # kind of duplicated
        self.cpu_info.common = self.gather_cpu_info_model(slugged_kv_list)
        self.cpu_info.processors = self.gather_processor_list(slugged_kv_list)

        # For now, 'hardware' is per
        self.cpu_info.other = self.gather_cpu_info_other(slugged_kv_list)

    def _capital_processor_to_model_name(self, item: List[str]) -> List[str]:
        """Use the uppercase Processor field value as the model name.

        For aarch64, the 'Processor' field is the closest to model name,
        so we sub it in now."""
        if item[0] == "Processor":
            item[0] = "model_name"
        return item

    def gather_processor_list(self, kv_list: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
        processor_list: List[Dict[str, Any]] = []
        for k, v in kv_list:
            if k != "processor":
                continue
            # build a ProcessorModel subclass for each processor
            # to add to CpuInfoModel.processors list
            cpu_info_model = self.gather_cpu_info_model(kv_list)
            cpu_info_model["processor"] = v
            processor_list.append(cpu_info_model)
        return processor_list

    # FIXME: more generic would be to split the stanzas by empty lines in the
    # first pass
    def gather_cpu_info_other(self, kv_list: List[Tuple[str, str]]) -> List[List[str]]:
        other_list: List[List[str]] = []
        for k, v in kv_list:
            if k == "hardware":
                other_list.append([k, v])
        return other_list

    def gather_cpu_info_model(self, kv_list: List[Tuple[str, str]]) -> Aarch64ProcessorModel:
        cpu_data = Aarch64ProcessorModel()
        for k, v in kv_list:
            if k == "processor" or k == "hardware":
                continue
            cpu_data[k] = v
        return cpu_data


class X86_64CpuInfo(BaseCpuInfo):
    def __init__(self):
        self.cpu_info = X86_64CpuinfoModel()

    def _parse(self, cpuinfo_data: str) -> None:
        # ordered list
        kv_iter: Iterator = split_key_value_generator(cpuinfo_data, line_splitter)

        processors: List[Dict[str, Any]] = []
        all_fields = set()
        for processor_stanza in split_kv_list_by_field(kv_iter, "processor"):
            proc_dict: X86_64ProcessorModel = self.processor_stanza_to_processor_data(processor_stanza)
            processors.append(proc_dict)
            # keep track of fields as we see them
            all_fields = accumulate_fields(all_fields, list(proc_dict.keys()))

        self.cpu_info.common = find_shared_key_value_pairs(all_fields, processors)
        self.cpu_info.processors = processors
        self.cpu_info.cpuinfo_data = cpuinfo_data

    def processor_stanza_to_processor_data(self, stanza: List[Tuple[str, str]]) -> X86_64ProcessorModel:
        "Take a list of k,v tuples, sluggify name, and add to a dict."
        cpu_data = X86_64ProcessorModel()
        cpu_data.update(dict([fact_sluggify_item(item) for item in stanza]))
        return cpu_data


class Ppc64CpuInfo(BaseCpuInfo):
    def __init__(self):
        self.cpu_info = Ppc64CpuinfoModel()

    def _parse(self, cpuinfo_data):
        kv_iter = split_key_value_generator(cpuinfo_data, line_splitter)

        processor_iter: Iterable[Tuple[str, Any]] = itertools.takewhile(self._not_timebase_key, kv_iter)
        for processor_stanza in split_kv_list_by_field(processor_iter, "processor"):
            proc_dict = Ppc64ProcessorModel.from_stanza(processor_stanza)
            self.cpu_info.processors.append(proc_dict)

        # Treat the rest of the info as shared between all of the processor entries
        # kv_iter is the rest of cpuinfo that isn't processor stanzas
        self.cpu_info.common = dict([fact_sluggify_item(item) for item in kv_iter])
        self.cpu_info.cpuinfo_data = cpuinfo_data

    def _not_timebase_key(self, item: str) -> bool:
        return item[0] != "timebase"


class SystemCpuInfoFactory:
    uname_to_cpuinfo = {
        "x86_64": X86_64CpuInfo,
        "aarch64": Aarch64CpuInfo,
        "ppc64": Ppc64CpuInfo,
        "ppc64le": Ppc64CpuInfo,
    }
    proc_cpuinfo_path = "/proc/cpuinfo"

    @classmethod
    def from_uname_machine(cls, uname_machine: str, prefix: Optional[str] = None) -> BaseCpuInfo:
        if uname_machine not in SystemCpuInfoFactory.uname_to_cpuinfo:
            # er?
            raise NotImplementedError

        proc_cpuinfo_string: str = cls.open_proc_cpuinfo(prefix)

        arch_class: type(BaseCpuInfo) = cls.uname_to_cpuinfo[uname_machine]
        return arch_class.from_proc_cpuinfo_string(proc_cpuinfo_string)

    @classmethod
    def open_proc_cpuinfo(cls, prefix: str = Optional[None]) -> str:
        proc_cpuinfo_path: str = cls.proc_cpuinfo_path
        if prefix:
            proc_cpuinfo_path = os.path.join(prefix, cls.proc_cpuinfo_path[1:])
        with open(proc_cpuinfo_path, "r") as proc_cpuinfo_f:
            return proc_cpuinfo_f.read()
