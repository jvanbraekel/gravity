"""
"""
import errno
import os
import shlex
import subprocess
from glob import glob

from gravity.io import debug, exception, info, warn
from gravity.process_manager import BaseProcessManager
from gravity.settings import ProcessManager

SYSTEMD_SERVICE_TEMPLATES = {}
SYSTEMD_SERVICE_TEMPLATE = """;
; This file is maintained by Gravity - CHANGES WILL BE OVERWRITTEN
;

[Unit]
Description=Galaxy {program_name}
After=network.target
After=time-sync.target

[Service]
UMask={galaxy_umask}
Type=simple
{systemd_user_group}
WorkingDirectory={galaxy_root}
TimeoutStartSec={settings[start_timeout]}
TimeoutStopSec={settings[stop_timeout]}
ExecStart={command}
#ExecReload=
#ExecStop=
{environment}
#MemoryLimit=
Restart=always

MemoryAccounting=yes
CPUAccounting=yes
BlockIOAccounting=yes

[Install]
WantedBy=multi-user.target
"""


class SystemdProcessManager(BaseProcessManager):

    name = ProcessManager.systemd

    def __init__(self, state_dir=None, start_daemon=True, foreground=False, **kwargs):
        super(SystemdProcessManager, self).__init__(state_dir=state_dir, **kwargs)
        self.user_mode = not self.config_manager.is_root

    @property
    def __systemd_unit_dir(self):
        unit_path = os.environ.get("GRAVITY_SYSTEMD_UNIT_PATH")
        if not unit_path:
            unit_path = "/etc/systemd/system" if not self.user_mode else os.path.expanduser("~/.config/systemd/user")
        return unit_path

    @property
    def __use_instance(self):
        return not self.config_manager.single_instance

    def __systemctl(self, *args, ignore_rc=None, capture=False, **kwargs):
        args = list(args)
        call = subprocess.check_call
        extra_args = os.environ.get("GRAVITY_SYSTEMCTL_EXTRA_ARGS")
        if extra_args:
            args = shlex.split(extra_args) + args
        if self.user_mode:
            args = ["--user"] + args
        debug("Calling systemctl with args: %s", args)
        if capture:
            call = subprocess.check_output
        try:
            return call(["systemctl"] + args, text=True)
        except subprocess.CalledProcessError as exc:
            if ignore_rc is None or exc.returncode not in ignore_rc:
                raise

    def __journalctl(self, *args, **kwargs):
        args = list(args)
        if self.user_mode:
            args = ["--user"] + args
        debug("Calling journalctl with args: %s", args)
        subprocess.check_call(["journalctl"] + args)

    def _service_default_path(self):
        environ = self.__systemctl("show-environment", capture=True)
        for line in environ.splitlines():
            if line.startswith("PATH="):
                return line.split("=", 1)[1]

    def _service_environment_formatter(self, environment, format_vars):
        return "\n".join("Environment={}={}".format(k, shlex.quote(v.format(**format_vars))) for k, v in environment.items())

    def terminate(self):
        # this is used to stop a foreground supervisord in the supervisor PM, so it is a no-op here
        pass

    def __unit_name(self, instance_name, service):
        unit_name = f"{service['config_type']}-"
        if self.__use_instance:
            unit_name += f"{instance_name}-"
        unit_name += f"{service['service_name']}.service"
        return unit_name

    def __update_service(self, config, service, instance_name):
        attribs = config.attribs
        program_name = service["service_name"]
        unit_name = self.__unit_name(instance_name, service)

        # under supervisor we expect that gravity is installed in the galaxy venv and the venv is active when gravity
        # runs, but under systemd this is not the case. we do assume $VIRTUAL_ENV is the galaxy venv if running as an
        # unprivileged user, though.
        virtualenv_dir = attribs.get("virtualenv")
        environ_virtual_env = os.environ.get("VIRTUAL_ENV")
        if not virtualenv_dir and self.user_mode and environ_virtual_env:
            warn(f"Assuming Galaxy virtualenv is value of $VIRTUAL_ENV: {environ_virtual_env}")
            warn("Set `virtualenv` in Gravity configuration to override")
            virtualenv_dir = environ_virtual_env
        elif not virtualenv_dir:
            exception("The `virtualenv` Gravity config option must be set when using the systemd process manager")

        # systemd-specific format vars
        systemd_format_vars = {
            "virtualenv_bin": f'{os.path.join(virtualenv_dir, "bin")}{os.path.sep}' if virtualenv_dir else "",
        }
        if not self.user_mode:
            systemd_format_vars["systemd_user_group"] = f"User={attribs['galaxy_user']}"
            if attribs["galaxy_group"] is not None:
                systemd_format_vars["systemd_user_group"] += f"\nGroup={attribs['galaxy_group']}"

        format_vars = self._service_format_vars(config, service, program_name, systemd_format_vars)

        # FIXME: bit of a hack
        if not format_vars["command"].startswith("/"):
            format_vars["command"] = f"{virtualenv_bin}/{format_vars['command']}"

        conf = os.path.join(self.__systemd_unit_dir, unit_name)

        # FIXME: dedup below
        template = SYSTEMD_SERVICE_TEMPLATE
        contents = template.format(**format_vars)
        self._update_file(conf, contents, unit_name, "service")

        return conf

    def follow(self, configs=None, service_names=None, quiet=False):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        u_args = [i for sl in list(zip(["-u"] * len(unit_names), unit_names)) for i in sl]
        self.__journalctl("-f", *u_args)

    def _process_config(self, config, **kwargs):
        """ """
        instance_name = config["instance_name"]
        intended_configs = set()

        try:
            os.makedirs(self.__systemd_unit_dir)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise

        for service in config["services"]:
            intended_configs.add(self.__update_service(config, service, instance_name))

        return intended_configs

    def _process_configs(self, configs):
        intended_configs = set()

        for config in configs:
            intended_configs = intended_configs | self._process_config(config)

        # the unit dir might not exist if $GRAVITY_SYSTEMD_UNIT_PATH is set (e.g. for tests), but this is fine if there
        # are no intended configs
        if not intended_configs and not os.path.exists(self.__systemd_unit_dir):
            return

        # FIXME: should use config_type, but that's per-service
        _present_configs = filter(
            lambda f: f.startswith("galaxy-") and f.endswith(".service"),
            os.listdir(self.__systemd_unit_dir))
        present_configs = set([os.path.join(self.__systemd_unit_dir, f) for f in _present_configs])

        for file in (present_configs - intended_configs):
            unit_name = os.path.basename(file)
            self.__systemctl("disable", "--now", unit_name)
            info("Removing service config %s", file)
            os.unlink(file)

    def __unit_names(self, configs, service_names):
        unit_names = []
        for config in configs:
            services = config.services
            if service_names:
                services = [s for s in config.services if s["service_name"] in service_names]
            unit_names.extend([self.__unit_name(config.instance_name, s) for s in services])
        return unit_names

    def start(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("enable", "--now", *unit_names)

    def stop(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("stop", *unit_names)

    def restart(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("restart", *unit_names)

    def reload(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("reload", *unit_names)

    def graceful(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("reload", *unit_names)

    def status(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("status", "--lines=0", *unit_names, ignore_rc=(3,))

    def update(self, configs=None, force=False, **kwargs):
        """ """
        if force:
            for config in configs:
                service_units = glob(os.path.join(self.__systemd_unit_dir, f"{config.config_type}-*.service"))
                # TODO: would need to add targets here assuming we add one
                if service_units:
                    newline = '\n'
                    info(f"Removing systemd units due to --force option:{newline}{newline.join(service_units)}")
                    list(map(os.unlink, service_units))
        self._process_configs(configs)
        # FIXME BEFORE RELEASE: only reload if there are changes
        self.__systemctl("daemon-reload")

    def shutdown(self):
        """ """
        if self.__use_instance:
            # we could use galaxy-*.service but this only shuts down the instances managed by *this* gravity
            configs = self.config_manager.get_registered_configs(process_manager=self.name)
            self.__systemctl("stop", *[f"galaxy-{c.instance_name}-*.service" for c in configs])
        else:
            self.__systemctl("stop", "galaxy-*.service")

    def pm(self, *args):
        """ """
        self.__systemctl(*args)
