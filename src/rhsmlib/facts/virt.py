# Probe hardware info that requires root
#
# Copyright (c) 2010-2016 Red Hat, Inc.
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
import logging
import string
import subprocess
import os
from typing import Dict, List, TextIO, Optional, Union

from rhsmlib.facts import collector

log = logging.getLogger(__name__)


class VirtWhatCollector(collector.FactsCollector):
    def get_all(self) -> Dict[str, Union[str, bool]]:
        return self.get_virt_info()

    # NOTE/TODO/FIXME: Not all platforms require admin privileges to determine virt type or uuid
    def get_virt_info(self) -> Dict[str, Union[str, bool]]:
        virt_dict: Dict[str, Union[str, bool]] = {}

        try:
            host_type_raw: bytes = subprocess.check_output("/usr/sbin/virt-what")
            host_type: str = host_type_raw.decode("utf-8")
            # BZ1018807 xen can report xen and xen-hvm.
            # Force a single line
            host_type = ", ".join(host_type.splitlines())

            # If this is blank, then not a guest
            virt_dict["virt.is_guest"] = bool(host_type)
            if bool(host_type):
                virt_dict["virt.is_guest"] = True
                virt_dict["virt.host_type"] = host_type
            else:
                virt_dict["virt.is_guest"] = False
                virt_dict["virt.host_type"] = "Not Applicable"
        # TODO:  Should this only catch OSErrors?
        except Exception as e:
            # Otherwise there was an error running virt-what - who knows
            log.exception(e)
            virt_dict["virt.is_guest"] = "Unknown"

        # xen dom0 is a guest for virt-what's purposes, but is a host for
        # our purposes. Adjust is_guest accordingly. (#757697)
        try:
            if virt_dict["virt.host_type"].find("dom0") > -1:
                virt_dict["virt.is_guest"] = False
        except KeyError:
            # if host_type is not defined, do nothing (#768397)
            pass

        return virt_dict


class VirtUuidCollector(collector.FactsCollector):
    # Note: unlike system uuid in DMI info, the virt.uuid is
    # available to non-root users on ppc64*
    # ppc64 LPAR has it's virt.uuid in /proc/devicetree
    # so parts of this don't need to be in AdminHardware
    devicetree_vm_uuid_arches: List[str] = ["ppc64", "ppc64le"]

    # No virt.uuid equiv is available for guests on these hypervisors
    no_uuid_platforms: List[str] = ["powervm_lx86", "xen-dom0", "ibm_systemz"]

    def get_all(self) -> Dict[str, str]:
        return self.get_virt_uuid()

    def get_virt_uuid(self) -> Dict[str, str]:
        """
        Given a populated fact list, add on a virt.uuid fact if appropriate.
        Partially adapted from Spacewalk's rhnreg.py, example hardware reporting
        found in virt-what tests
        """

        # For 99% of uses, virt.uuid will actually be from dmi info
        virt_uuid_dict: Dict[str, str] = {}

        if self._collected_hw_info and "dmi.system.uuid" in self._collected_hw_info:
            virt_uuid_dict["virt.uuid"] = self._collected_hw_info["dmi.system.uuid"]

        # ie, ppc64/ppc64le
        if self.arch in self.devicetree_vm_uuid_arches:
            uuid: Optional[str] = self._get_devicetree_uuid()
            if uuid is not None:
                virt_uuid_dict["virt.uuid"] = uuid

        # potentially override DMI-determined UUID with
        # what is on the file system (xen para-virt)
        # Does this need root access?
        try:
            uuid_file: TextIO = open("/sys/hypervisor/uuid", "r")
            uuid = uuid_file.read()
            uuid_file.close()
            virt_uuid_dict["virt.uuid"] = uuid.rstrip("\r\n")
        except IOError:
            pass

        return virt_uuid_dict

    def _get_devicetree_uuid(self) -> Optional[str]:
        """
        Collect the virt.uuid fact from device-tree.

        For ppc64/ppc64le systems running KVM or PowerKVM, the
        virt uuid is found in /proc/device-tree/vm,uuid.

        For ppc64/ppc64le LPARs, the UUID is found in
        /proc/device-tree/ibm,partition-uuid.

        (In contrast to use of DMI on x86_64).
        """

        uuid_paths: List[str] = [
            f"{self.prefix}/proc/device-tree/vm,uuid",
            f"{self.prefix}/proc/device-tree/ibm,partition-uuid",
        ]

        for uuid_path in uuid_paths:
            if not os.path.isfile(uuid_path):
                continue
            try:
                with open(uuid_path) as fo:
                    contents = fo.read()
                    # Apparently ppc64 can report a virt uuid with a null byte at the end.
                    # See BZ 1405125.
                    vm_uuid: str = contents.strip(string.whitespace + "\0")
                    return vm_uuid
            except IOError as e:
                log.warning("Tried to read %s but there was an error: %s", uuid_path, e)

        log.warning("No available file for UUID on %s", self.arch)
        return None


class VirtCollector(collector.FactsCollector):
    def get_all(self) -> Dict[str, Union[str, bool]]:
        virt_info: Dict[str, Union[str, bool]] = {}

        virt_what_collector = VirtWhatCollector(prefix=self.prefix, testing=self.testing)
        virt_what_info: Dict[str, Union[str, bool]] = virt_what_collector.get_all()
        virt_info.update(virt_what_info)

        # Ensure we do not gather the virt.uuid fact for host_types we cannot
        # See BZ#1438085
        for no_uuid_host_type in VirtUuidCollector.no_uuid_platforms:
            if (
                virt_what_info.get("virt.host_type", None) is None
                or virt_what_info["virt.host_type"].find(no_uuid_host_type) > -1
            ):
                return virt_info

        if virt_what_info["virt.is_guest"]:
            virt_uuid_collector = VirtUuidCollector(
                prefix=self.prefix, testing=self.testing, collected_hw_info=self._collected_hw_info
            )
            virt_uuid_info = virt_uuid_collector.get_all()
            virt_info.update(virt_uuid_info)
        return virt_info
