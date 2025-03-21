# Copyright (c) 2011 - 2012 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
import _io
import logging

import importlib.util
import rpm
import os
from typing import List, Union

from rhsm import ourjson as json
from rhsm.utils import suppress_output
from rhsm.repofile import get_repo_file_classes
from cloud_what import provider

try:
    from rhsm.repofile import dnf
except ImportError:
    dnf = None

try:
    from rhsm.repofile import libdnf
except ImportError:
    libdnf = None

try:
    from rhsm.repofile import yum
except ImportError:
    yum = None

try:
    from rhsm.repofile import apt
except ImportError:
    apt = None

use_zypper: bool = importlib.util.find_spec("zypp_plugin") is not None


if use_zypper:
    REPOSITORY_PATH = "/etc/rhsm/zypper.repos.d/redhat.repo"
else:
    REPOSITORY_PATH = "/etc/yum.repos.d/redhat.repo"

log = logging.getLogger(__name__)


class InvalidProfileType(Exception):
    """
    Thrown when attempting to get a profile of an unsupported type.
    """

    pass


class ModulesProfile:
    def __init__(self) -> None:
        self.content: List[dict] = self.__generate()

    def __str__(self) -> str:
        return str(self.content)

    def __eq__(self, other: "ModulesProfile") -> bool:
        return self.content == other.content

    @staticmethod
    def _uniquify(module_list: list) -> list:
        """
        Try to uniquify list of modules, when there are duplicated repositories
        :param module_list: list of modules
        :return: List of modules without duplicity
        """
        ret = {}
        for module in module_list:
            key = (module["name"], module["stream"], module["version"], module["context"], module["arch"])
            # Prefer duplicates that are Active.
            # There are "enabled" duplicates from dnf that are marked as inactive.
            if key not in ret:
                ret[key] = module
            else:
                if ret[key].get("active", False) is False and module.get("active", False) is True:
                    ret[key] = module

        return list(ret.values())

    @staticmethod
    def fix_aws_rhui_repos(base) -> None:
        """
        Try to fix RHUI repos on AWS systems. When the system is running on AWS, then we have
        to fix repository URL. See: https://bugzilla.redhat.com/show_bug.cgi?id=1924126
        :param base: DNF base
        :return: None
        """
        # First try to detect if system is running on AWS
        cloud_provider = provider.get_cloud_provider()

        if cloud_provider is None or cloud_provider.CLOUD_PROVIDER_ID != "aws":
            log.debug("This system is not running on AWS. Skipping fixing of RHUI repos.")
            return

        log.debug("This system is running on AWS. Trying to collect AWS metadata")
        metadata_str = cloud_provider.get_metadata()
        if metadata_str is None:
            log.debug("Unable to gather metadata from IMDS. Skipping fixing of RHUI repos.")
            return

        try:
            metadata = json.loads(metadata_str)
        except json.JSONDecodeError:
            log.warning("AWS provided corrupted json metadata document. Skipping fixing of RHUI repos.")
            return

        if "region" not in metadata:
            log.debug("Region is not specified in AWS metadata. Skipping fixing of RHUI repos.")
            return

        region = metadata["region"]
        log.debug(f"Trying to fix URLs of RHUI repos using region: {region}")
        for repo in cloud_provider.rhui_repos(base):
            log.debug(f"Trying to fix repository: {repo.id}")
            try:
                cloud_provider.fix_rhui_url_template(repo, region)
            except ValueError as error:
                log.debug("Unable to fix RHUI URL: {error}".format(error=error))

    @suppress_output
    def __generate(self) -> List[dict]:
        module_list = []
        if dnf is not None and libdnf is not None:
            base: dnf.Base = dnf.Base()

            # Read yum/dnf variables from <install_root>/etc/yum/vars and <install_root>/etc/dnf/vars
            # See: https://bugzilla.redhat.com/show_bug.cgi?id=1863039
            base.conf.substitutions.update_from_etc(base.conf.installroot)
            base.read_all_repos()

            # Try to fix repo names, when AWS cloud provider is detected
            self.fix_aws_rhui_repos(base)

            try:
                log.debug("Trying to fill dnf sack object...")
                base.fill_sack()
            except dnf.exceptions.RepoError as err:
                log.error("Unable to create sack object: %s" % str(err))
                return []
            # FIXME: DNF doesn't provide public API for modulemd
            try:
                modules = base._moduleContainer
            except AttributeError:
                log.warning("DNF does not provide modulemd functionality")
                return []
            all_module_list = modules.getModulePackages()

            for module_pkg in all_module_list:
                status = "default"
                active = False
                if modules.isEnabled(module_pkg.getName(), module_pkg.getStream()):
                    status = "enabled"
                elif modules.isDisabled(module_pkg.getName()):
                    status = "disabled"
                if modules.isModuleActive(module_pkg.getId()):
                    active = True
                installed_profiles = []
                if status == "enabled":
                    # It has to be list, because we compare this with cached json document and
                    # JSON does not support anything like a tuple :-)
                    installed_profiles = list(modules.getInstalledProfiles(module_pkg.getName()))
                module_list.append(
                    {
                        "name": module_pkg.getName(),
                        "stream": module_pkg.getStream(),
                        "version": module_pkg.getVersion(),
                        "context": module_pkg.getContext(),
                        "arch": module_pkg.getArch(),
                        "profiles": [profile.getName() for profile in module_pkg.getProfiles()],
                        "installed_profiles": installed_profiles,
                        "status": status,
                        "active": active,
                    }
                )

        return ModulesProfile._uniquify(module_list)

    def collect(self) -> List[dict]:
        """
        Gather list of modules reported to candlepin server
        :return: List of modules
        """
        return self.content


class EnabledReposProfile:
    """
    Collect information about enabled repositories
    """

    def __init__(self, repo_file: str = REPOSITORY_PATH) -> None:
        self._content = []

        directory_path = os.path.dirname(repo_file)
        file_name = os.path.basename(repo_file)

        for repo_file_cls, _ in get_repo_file_classes():
            repo = repo_file_cls(directory_path, file_name)
            repo.read()
            self._content.extend(repo.enabled_repos())
        self._content.sort(key=lambda x: x["baseurl"])

    def __eq__(self, other: "EnabledReposProfile") -> bool:
        return self._content == other._content

    def collect(self) -> List[dict]:
        """
        Gather list of enabled repositories
        :return: List of enabled repositories
        """
        return self._content


class Package:
    """
    Represents a package installed on the system.
    """

    def __init__(
        self, name: str, version: str, release: str, arch: str, epoch: int = 0, vendor: str = None
    ) -> None:
        self.name: str = name
        self.version: str = version
        self.release: str = release
        self.arch: str = arch
        self.epoch: int = epoch
        self.vendor: str = vendor

    def to_dict(self) -> dict:
        """Returns a dict representation of this package info."""
        return {
            "name": self._normalize_string(self.name),
            "version": self._normalize_string(self.version),
            "release": self._normalize_string(self.release),
            "arch": self._normalize_string(self.arch),
            "epoch": self._normalize_string(self.epoch),
            "vendor": self._normalize_string(self.vendor),  # bz1519512 handle vendors that aren't utf-8
        }

    def __eq__(self, other: "Package") -> bool:
        """
        Compare one profile to another to determine if anything has changed.
        """
        if not isinstance(self, type(other)):
            return False

        if (
            self.name == other.name
            and self.version == other.version
            and self.release == other.release
            and self.arch == other.arch
            and self.epoch == other.epoch
            and self._normalize_string(self.vendor) == self._normalize_string(other.vendor)
        ):
            return True

        return False

    def __str__(self) -> str:
        return "<Package: %s %s %s>" % (self.name, self.version, self.release)

    # added in support of bz1519512, bz1543639
    @staticmethod
    def _normalize_string(value: Union[str, bytes]) -> str:
        if type(value) is bytes:
            return value.decode("utf-8", "replace")
        return value


class RPMProfile:
    def __init__(self, from_file: _io.TextIOWrapper = None) -> None:
        """
        Load the RPM package profile from a given file, or from rpm itself.

        NOTE: from_file is a file descriptor, not a file name.
        """
        self.packages = []
        if from_file:
            log.debug("Loading RPM profile from file: %s" % from_file.name)
            json_buffer = from_file.read()
            pkg_dicts = json.loads(json_buffer)
            for pkg_dict in pkg_dicts:
                self.packages.append(
                    Package(
                        name=pkg_dict["name"],
                        version=pkg_dict["version"],
                        release=pkg_dict["release"],
                        arch=pkg_dict["arch"],
                        epoch=pkg_dict["epoch"],
                        vendor=pkg_dict["vendor"],
                    )
                )
        else:
            log.debug("Loading current RPM profile.")
            ts = rpm.TransactionSet()
            ts.setVSFlags(-1)
            installed = ts.dbMatch()
            self.packages = self._accumulate_profile(installed)

    @staticmethod
    def _accumulate_profile(rpm_header_list: List[dict]) -> List[Package]:
        """
        Accumulates list of installed rpm info
        @param rpm_header_list: list of rpm headers
        @return: list of package info dicts
        """

        pkg_list = []
        for h in rpm_header_list:
            if h["name"] == "gpg-pubkey":
                # dbMatch includes imported gpg keys as well
                # skip these for now as there isn't compelling
                # reason for server to know this info
                continue
            pkg_list.append(
                Package(
                    name=h["name"],
                    version=h["version"],
                    release=h["release"],
                    arch=h["arch"],
                    epoch=h["epoch"] or 0,
                    vendor=h["vendor"] or None,
                )
            )
        return pkg_list

    def collect(self) -> List[dict]:
        """
        Returns a list of dicts containing the package info.

        See 'packages' member on this object for a list of actual objects.

        @return : list of package info dicts
        @rtype: list
        """
        pkg_dicts = []
        for pkg in self.packages:
            pkg_dicts.append(pkg.to_dict())
        return pkg_dicts

    def __eq__(self, other: "RPMProfile") -> bool:
        """
        Compare one profile to another to determine if anything has changed.
        """
        if not isinstance(self, type(other)):
            return False

        # Quickly check if we have a different number of packages for an
        # easy answer before we start checking everything:
        if len(self.packages) != len(other.packages):
            return False

        for pkg in self.packages:
            if pkg not in other.packages:
                return False

        return True


class DebProfile(object):
    def __init__(self):
        cache = apt.Cache()
        self._deb_profile = [
            {
                "name": package.name,
                "version": package.installed.version,
                "architecture": package.installed.architecture,
            }
            for package in cache
            if package.installed is not None
        ]

    def __eq__(self, other):
        """
        Compare one profile to another to determine if anything has changed.
        """
        if not isinstance(self, type(other)):
            return False

        return self._deb_profile == other._deb_profile

    def collect(self):
        return self._deb_profile


def get_profile(profile_type: str):
    """
    Returns an instance of a Profile object
    @param profile_type: profile type
    Returns an instance of a Profile object
    """
    if profile_type not in PROFILE_MAP:
        raise InvalidProfileType("Could not find profile for type [%s]", profile_type)
    profile = PROFILE_MAP[profile_type]()
    return profile


# Profile types we support:
PROFILE_MAP: dict = {
    "enabled_repos": EnabledReposProfile,
}

if dnf is not None or yum is not None:
    PROFILE_MAP["rpm"] = RPMProfile
    PROFILE_MAP["modulemd"] = ModulesProfile

if apt is not None:
    PROFILE_MAP["deb"] = DebProfile
