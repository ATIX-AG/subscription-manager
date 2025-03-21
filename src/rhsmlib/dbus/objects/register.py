# Copyright (c) 2016 Red Hat, Inc.
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
import json
import logging
import threading
from typing import Callable, List, Optional, TYPE_CHECKING

import dbus
import dbus.service
import subscription_manager.injection as inj

from rhsmlib.dbus import constants, exceptions, dbus_utils, base_object, server, util
from rhsmlib.services.register import RegisterService
from rhsmlib.services.unregister import UnregisterService
from rhsmlib.services.entitlement import EntitlementService
from rhsmlib.services.environment import EnvironmentService
from rhsmlib.services.config import Config
from rhsmlib.client_info import DBusSender
from subscription_manager.cp_provider import CPProvider

from subscription_manager.i18n import Locale
from subscription_manager.i18n import ugettext as _
from subscription_manager.utils import is_true_value

if TYPE_CHECKING:
    from rhsm.connection import UEPConnection
    from rhsm.config import RhsmConfigParser

log = logging.getLogger(__name__)

ENVIRONMENTS_KEYS_TO_FILTER: List[str] = [
    "contentPrefix",
    "created",
    "environmentContent",
    "owner",
    "updated",
]


class RegisterDBusImplementation(base_object.BaseImplementation):
    def __init__(self):
        self.server: Optional[server.DomainSocketServer] = None
        self.lock = threading.Lock()
        self.started_event = threading.Event()
        self.stopped_event = threading.Event()

    def start(self, sender: str) -> str:
        """Start a D-Bus server listening on a domain socket instead of a System bus.

        :return: Server address.
        """
        with self.lock:
            # When some other application already started domain socket listener, then
            # write log message and return existing address
            if self.server is not None:
                log.debug(f"Domain socket listener already running, started by: {self.server.sender}")
                # Add sender to the list of senders using server
                log.debug(f"Adding another sender {sender} to the set of senders")
                self.server.add_sender(sender)
                return self.server.address

            log.debug("Trying to create new domain socket server.")
            self.server = server.DomainSocketServer(
                object_classes=[DomainSocketRegisterDBusObject],
                sender=sender,
                cmd_line=DBusSender.get_cmd_line(sender),
            )
            address: str = self.server.run()
            log.debug(
                f"Domain socket server for sender {sender} is created and listens on address '{address}'."
            )
            return address

    def stop(self, sender: str) -> bool:
        """Stop the server running on the domain socket.

        :raises exceptions.Failed: No domain socket server is running.
        """
        with self.lock:
            if self.server is None:
                raise exceptions.Failed("No domain socket server is running")

            # Remove current sender and check if other senders are still running.
            # If there is at least one sender using this server still running, then
            # only return False
            self.server.remove_sender(sender)
            if self.server.are_other_senders_still_running() is True:
                log.debug("Not stopping domain socket server, because some senders still uses it.")
                return False

            self.server.shutdown()
            self.server = None
            log.debug("Domain socket server stopped.")
            return True


class RegisterDBusObject(base_object.BaseObject):
    default_dbus_path = constants.REGISTER_DBUS_PATH
    interface_name = constants.REGISTER_INTERFACE

    def __init__(self, conn=None, object_path=None, bus_name=None):
        super().__init__(conn=conn, object_path=object_path, bus_name=bus_name)
        self.impl = RegisterDBusImplementation()

    @util.dbus_service_method(
        constants.REGISTER_INTERFACE,
        in_signature="s",
        out_signature="s",
    )
    @util.dbus_handle_sender
    @util.dbus_handle_exceptions
    def Start(self, locale, sender=None):
        locale = dbus_utils.dbus_to_python(locale, expected_type=str)
        Locale.set(locale)

        address: str = self.impl.start(sender)
        return address

    @util.dbus_service_method(
        constants.REGISTER_INTERFACE,
        in_signature="s",
        out_signature="b",
    )
    @util.dbus_handle_sender
    @util.dbus_handle_exceptions
    def Stop(self, locale, sender=None) -> bool:
        locale = dbus_utils.dbus_to_python(locale, expected_type=str)
        Locale.set(locale)

        return self.impl.stop(sender)


class OrgNotSpecifiedException(dbus.DBusException):
    """
    This exception is intended for signaling that user is member of more
    organizations and no organization was specified
    """

    _dbus_error_name = "%s.Error" % constants.REGISTER_INTERFACE
    include_traceback = False
    severity = "info"

    def __init__(self, username):
        self.username = username

    def __str__(self):
        return _("User %s is member of more organizations, but no organization was selected") % self.username


class NoOrganizationException(dbus.DBusException):
    """
    This exception is intended for signaling that user is not member of any
    organization and such user cannot register to candlepin server.
    """

    _dbus_error_name = "%s.Error" % constants.REGISTER_INTERFACE
    include_traceback = False

    def __init__(self, username):
        self.username = username

    def __str__(self):
        return _("User %s is not member of any organization") % self.username


class DomainSocketRegisterDBusImplementation(base_object.BaseImplementation):
    _multiple_organizations_signal: Callable[[object, str], None] = lambda self, organizations: None
    """A callback function to call by _on_multiple_organizations.

    An actual implementation is provided by DomainSocketRegisterDBusObject,
    where a D-Bus signal is emitted.
    """

    def __init__(self, parser: "RhsmConfigParser" = None):
        self.config = Config(parser)

    def get_organizations(self, options: dict) -> List[dict]:
        """Get user account organizations.

        :param options: Connection options including the 'username' and 'password' keys.
        :return: List of organizations.
        """
        uep: UEPConnection = self.build_uep(options, basic_auth_method=True)
        owners: List[dict] = uep.getOwnerList(options["username"])
        return owners

    def get_environments(self, options: dict) -> List[dict]:
        """Get environments for every org belonging to user

        :param options: Connection options including the 'username', 'password', and 'org_id' keys
        :return: List of environments
        """
        uep: UEPConnection = self.build_uep(options, basic_auth_method=True)
        environment_service: EnvironmentService = EnvironmentService(uep)

        environments = environment_service.list(options["org_id"])

        for environment in environments:
            for key in ENVIRONMENTS_KEYS_TO_FILTER:
                environment.pop(key, None)
            if ("type" not in environment) or (environment["type"] is None):
                environment["type"] = ""

        return environments

    def register_with_credentials(
        self, organization: Optional[str], register_options: dict, connection_options: dict
    ) -> dict:
        """Register the system.

        :param organization: An organization user is part of, if they is more than one.
        :param register_options: Registration options passed to the RegisterService.
        :param connection_options: Connection options including the 'username' and 'password' keys.

        :raises dbus.DBusException: The system is already registered.
        :raises OrgNotSpecifiedException: User is part of multiple organizations, but none was specified.
        """
        self._check_force_handling(register_options, connection_options)

        uep: UEPConnection = self.build_uep(connection_options)
        service = RegisterService(uep)

        if not organization:
            organization: str = service.determine_owner_key(
                username=connection_options["username"],
                get_owner_cb=self._on_multiple_organizations,
                no_owner_cb=self._on_no_organization,
            )

        # If there is more organizations, a signal was triggered in _get_owner_cb, and None was returned.
        # However, we still have to raise something to prevent the registration.
        if not organization:
            raise OrgNotSpecifiedException(username=connection_options["username"])

        enable_content: bool = self._remove_enable_content_option(register_options)
        consumer: dict = service.register(organization, **register_options)

        # When consumer is created, we can try to enable content, if requested.
        if enable_content:
            self._enable_content(uep, consumer)
        else:
            self._disable_content()

        return consumer

    def register_with_activation_keys(
        self, organization: Optional[str], register_options: dict, connection_options: dict
    ) -> dict:
        """Register the system.

        :param organization: An organization user is part of, if there is more than one.
        :param register_options: Registration options passed to the RegisterService.
        :param connection_options: Connection options.
        """

        self._check_force_handling(register_options, connection_options)

        uep: UEPConnection = self.build_uep(connection_options)
        service = RegisterService(uep)

        enable_content: bool = self._remove_enable_content_option(register_options, default=True)
        consumer: dict = service.register(organization, **register_options)

        # When consumer is created, we can try to enable content, if requested.
        if enable_content:
            self._enable_content(uep, consumer)
        else:
            self._disable_content()

        return consumer

    def _on_multiple_organizations(self, owners: List[dict]) -> None:
        """A function to call when a member is part of multiple organizations.

        Invokes a callback.
        """
        organizations: str = json.dumps(owners)
        self._multiple_organizations_signal(organizations)

    def _on_no_organization(self, username: str) -> None:
        """A function to call when a member is not part of any organization.

        :raises NoOrganizationException:
        """
        raise NoOrganizationException(username=username)

    @staticmethod
    def _remove_enable_content_option(options: dict, default: bool = False) -> bool:
        """Try to remove enable_content option from options dictionary.

        :returns: The value of 'enable_content' key.
        """
        if "enable_content" not in options:
            return default

        enable_content = options.pop("enable_content")
        return is_true_value(enable_content)

    def _enable_content(self, uep: "UEPConnection", consumer: dict) -> None:
        """
        Try to enable generating content from SCA certificates. This method
        does several things:
        1) It sets the value of manage_repos to 1 in rhsm.conf so it will
           be possible to generate redhat.repo file from SCA certificate now
           or later by subscription-manager or rhsmcertd.
        2) It downloads SCA certificate and generate redhat.repo file
        """
        content_access: str = consumer["owner"]["contentAccessMode"]

        if content_access == "entitlement":
            log.error("Entitlement content access mode is not supported")
        elif content_access == "org_environment":
            log.info("Enabling generating content (redhat.repo) from SCA certificate in rhsm.conf.")
            self.config["rhsm"]["manage_repos"] = "1"
            self.config.persist()
            log.debug("Refreshing since 'enable_content' is true.")
            service = EntitlementService(uep)
            # TODO: try get anything useful from refresh result. It is not possible atm.
            service.refresh(remove_cache=False, force=False)
        else:
            log.error(f"Unable to enable content due to unsupported content access mode: '{content_access}'")

    def _disable_content(self) -> None:
        """
        Try to disable generating content from SCA certificates. This method
        sets the value of manage_repos to 0 in rhsm.conf to avoid generating redhat.repo
        file from SCA certificates by subscription-manager or rhsmcertd.
        """
        log.info("Disabling generating content (redhat.repo) from SCA certificate in rhsm.conf.")
        self.config["rhsm"]["manage_repos"] = "0"
        self.config.persist()

    def _check_force_handling(self, register_options: dict, connection_options: dict) -> None:
        """
        Handles "force=true" in the registration options

        :param register_options: Registration options passed to the RegisterService.
        :param connection_options: Connection options.

        :raises dbus.DBusException: The system is already registered, or the unregistration failed.
        """
        if not self.is_registered():
            return

        if register_options.get("force", False):
            self._unregister(connection_options)
        else:
            raise dbus.DBusException("This system is already registered.")

    def _unregister(self, options: dict) -> None:
        """Unregister the system and clean CPProvider.

        :param options: Connection options.
        """
        self.ensure_registered()
        log.info("This system is already registered, attempting to unregister.")

        uep: UEPConnection = self.build_uep(options)
        UnregisterService(uep).unregister()

        # The CPProvider must be cleaned and CP object must be reinitialized to
        # handle authorization after unregistration.
        cp_provider: CPProvider = inj.require(inj.CP_PROVIDER)
        cp_provider.clean()


class DomainSocketRegisterDBusObject(base_object.BaseObject):
    interface_name = constants.PRIVATE_REGISTER_INTERFACE
    default_dbus_path = constants.PRIVATE_REGISTER_DBUS_PATH

    def __init__(self, conn=None, object_path=None, bus_name=None, sender=None, cmd_line=None):
        # On our DomainSocket DBus server since a private connection is not a "bus", we have to treat
        # it slightly differently. In particular there are no names, no discovery and so on.
        super().__init__(conn=conn, object_path=object_path, bus_name=bus_name)
        self.impl = DomainSocketRegisterDBusImplementation()
        self.impl._multiple_organizations_signal = self.UserMemberOfOrgs

        self.sender = sender
        self.cmd_line = cmd_line

    @dbus.service.method(
        dbus_interface=constants.PRIVATE_REGISTER_INTERFACE,
        in_signature="sssa{sv}s",
        out_signature="s",
    )
    @util.dbus_handle_exceptions
    def GetEnvironments(self, username, password, org_id, connection_options, locale):
        """
        This method tries to return list of environments in the given orgs. This method also uses
        basic authentication method (using username and password).

        :param username: string with username used for connection to candlepin server
        :param password: string with password
        :param org_id: string with org id to list environments for
        :param connection_options: dictionary with connection options
        :param locale: string with locale
        :return: string with json returned by candlepin server
        """
        connection_options = dbus_utils.dbus_to_python(connection_options, expected_type=dict)
        connection_options["username"] = dbus_utils.dbus_to_python(username, expected_type=str)
        connection_options["password"] = dbus_utils.dbus_to_python(password, expected_type=str)
        connection_options["org_id"] = dbus_utils.dbus_to_python(org_id, expected_type=str)
        locale = dbus_utils.dbus_to_python(locale, expected_type=str)

        with DBusSender() as dbus_sender:
            dbus_sender.set_cmd_line(sender=self.sender, cmd_line=self.cmd_line)
            Locale.set(locale)

            environments = self.impl.get_environments(connection_options)

            dbus_sender.reset_cmd_line()

        return json.dumps(environments)

    @dbus.service.method(
        dbus_interface=constants.PRIVATE_REGISTER_INTERFACE,
        in_signature="ssa{sv}s",
        out_signature="s",
    )
    @util.dbus_handle_exceptions
    def GetOrgs(self, username, password, connection_options, locale):
        """
        This method tries to return list of organization user belongs to. This method also uses
        basic authentication method (using username and password).

        :param username: string with username used for connection to candlepin server
        :param password: string with password
        :param connection_options: dictionary with connection options
        :param locale: string with locale
        :return: string with json returned by candlepin server
        """
        connection_options = dbus_utils.dbus_to_python(connection_options, expected_type=dict)
        connection_options["username"] = dbus_utils.dbus_to_python(username, expected_type=str)
        connection_options["password"] = dbus_utils.dbus_to_python(password, expected_type=str)
        locale = dbus_utils.dbus_to_python(locale, expected_type=str)

        with DBusSender() as dbus_sender:
            dbus_sender.set_cmd_line(sender=self.sender, cmd_line=self.cmd_line)
            Locale.set(locale)

            owners = self.impl.get_organizations(connection_options)

            dbus_sender.reset_cmd_line()

        return json.dumps(owners)

    @util.dbus_service_signal(
        constants.PRIVATE_REGISTER_INTERFACE,
        signature="s",
    )
    @util.dbus_handle_exceptions
    def UserMemberOfOrgs(self, orgs):
        """
        Signal triggered, when user tries to register, no organization is specified, but
        user is member of more than one organization and it will be necessary to select
        one organization in client application consuming this D-Bus API
        :param orgs: string with json.dump of org dictionary
        :return: None
        """
        log.debug(
            "D-Bus signal UserMemberOfOrgs emitted on the interface %s with arg: %s"
            % (constants.PRIVATE_REGISTER_INTERFACE, orgs)
        )
        return None

    @dbus.service.method(
        dbus_interface=constants.PRIVATE_REGISTER_INTERFACE,
        in_signature="sssa{sv}a{sv}s",
        out_signature="s",
    )
    @util.dbus_handle_exceptions
    def Register(self, org, username, password, options, connection_options, locale):
        """
        This method registers the system using basic auth
        (username and password for a given org).
        For any option that is required but not included the default will be
        used.

        Options is a dict of strings that modify the outcome of this method.
        """
        org = dbus_utils.dbus_to_python(org, expected_type=str)
        connection_options = dbus_utils.dbus_to_python(connection_options, expected_type=dict)
        connection_options["username"] = dbus_utils.dbus_to_python(username, expected_type=str)
        connection_options["password"] = dbus_utils.dbus_to_python(password, expected_type=str)
        options = dbus_utils.dbus_to_python(options, expected_type=dict)
        locale = dbus_utils.dbus_to_python(locale, expected_type=str)

        with DBusSender() as dbus_sender:
            dbus_sender.set_cmd_line(sender=self.sender, cmd_line=self.cmd_line)
            Locale.set(locale)

            consumer: dict = self.impl.register_with_credentials(org, options, connection_options)

        return json.dumps(consumer)

    @dbus.service.method(
        dbus_interface=constants.PRIVATE_REGISTER_INTERFACE,
        in_signature="sasa{sv}a{sv}s",
        out_signature="s",
    )
    @util.dbus_handle_exceptions
    def RegisterWithActivationKeys(self, org, activation_keys, options, connection_options, locale):
        """
        This method registers the system using organization and activation keys
        """
        connection_options = dbus_utils.dbus_to_python(connection_options, expected_type=dict)
        options = dbus_utils.dbus_to_python(options, expected_type=dict)
        options["activation_keys"] = dbus_utils.dbus_to_python(activation_keys, expected_type=list)
        org = dbus_utils.dbus_to_python(org, expected_type=str)
        locale = dbus_utils.dbus_to_python(locale, expected_type=str)

        with DBusSender() as dbus_sender:
            dbus_sender.set_cmd_line(sender=self.sender, cmd_line=self.cmd_line)
            Locale.set(locale)

            consumer: dict = self.impl.register_with_activation_keys(org, options, connection_options)

        return json.dumps(consumer)
