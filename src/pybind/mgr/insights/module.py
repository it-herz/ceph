import datetime
import json
import re
import threading
import six
from mgr_module import MgrModule, CommandResult
from . import health as health_util

# hours of crash history to report
CRASH_HISTORY_HOURS = 24
# hours of health history to report
HEALTH_HISTORY_HOURS = 24
# how many hours of health history to keep
HEALTH_RETENTION_HOURS = 30
# health check name for insights health
INSIGHTS_HEALTH_CHECK = "MGR_INSIGHTS_WARNING"
# version tag for persistent data format
ON_DISK_VERSION = 1

class Module(MgrModule):
    COMMANDS = [
        {
            "cmd": "insights",
            "desc": "Retrieve insights report",
            "perm": "r",
            "poll": "false",
        },
        {
            'cmd': 'insights prune-health name=hours,type=CephString',
            'desc': 'Remove health history older than <hours> hours',
            'perm': 'rw',
            "poll": "false",
        },
    ]

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)

        self._shutdown = False
        self._evt = threading.Event()

        # health history tracking
        self._pending_health = []
        self._health_slot = None

    def notify(self, ttype, ident):
        """Queue updates for processing"""
        if ttype == "health":
            self.log.info("Received health check update {} pending".format(
                len(self._pending_health)))
            health = json.loads(self.get("health")["json"])
            self._pending_health.append(health)
            self._evt.set()

    def serve(self):
        self._health_reset()
        while True:
            self._evt.wait(health_util.PERSIST_PERIOD.total_seconds())
            self._evt.clear()
            if self._shutdown:
                break

            # when the current health slot expires, finalize it by flushing it to
            # the store, and initializing a new empty slot.
            if self._health_slot.expired():
                self.log.info("Health history slot expired {}".format(
                    self._health_slot))
                self._health_maybe_flush()
                self._health_reset()
                self._health_prune_history(HEALTH_RETENTION_HOURS)

            # fold in pending health snapshots and flush
            self.log.info("Applying {} health updates to slot {}".format(
                len(self._pending_health), self._health_slot))
            for health in self._pending_health:
                self._health_slot.add(health)
            self._pending_health = []
            self._health_maybe_flush()

    def shutdown(self):
        self._shutdown = True
        self._evt.set()

    def _health_reset(self):
        """Initialize the current health slot

        The slot will be initialized with any state found to have already been
        persisted, otherwise the slot will start empty.
        """
        key = health_util.HealthHistorySlot.curr_key()
        data = self.get_store(key)
        if data:
            init_health = json.loads(data)
            self._health_slot = health_util.HealthHistorySlot(init_health)
        else:
            self._health_slot = health_util.HealthHistorySlot()
        self.log.info("Reset curr health slot {}".format(self._health_slot))

    def _health_maybe_flush(self):
        """Store the health for the current time slot if needed"""

        self.log.info("Maybe flushing slot {} needed {}".format(
            self._health_slot, self._health_slot.need_flush()))

        if self._health_slot.need_flush():
            key = self._health_slot.key()

            # build store data entry
            slot = self._health_slot.health()
            assert "version" not in slot
            slot.update(dict(version = ON_DISK_VERSION))
            data = json.dumps(slot, cls=health_util.HealthEncoder)

            self.log.debug("Storing health key {} data {}".format(
                key, json.dumps(slot, indent=2, cls=health_util.HealthEncoder)))

            self.set_store(key, data)
            self._health_slot.mark_flushed()

    def _health_filter(self, f):
        """Filter hourly health reports timestamp"""
        matches = filter(
            lambda t: f(health_util.HealthHistorySlot.key_to_time(t[0])),
            six.iteritems(self.get_store_prefix(health_util.HEALTH_HISTORY_KEY_PREFIX)))
        return map(lambda t: t[0], matches)

    def _health_prune_history(self, hours):
        """Prune old health entries"""
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours = hours)
        for key in self._health_filter(lambda ts: ts <= cutoff):
            self.log.info("Removing old health slot key {}".format(key))
            self.set_store(key, None)

    def _health_report(self, hours):
        """
        Report a consolidated health report for the past N hours.
        """
        # roll up the past N hours of health info
        collector = health_util.HealthHistorySlot()
        keys = health_util.HealthHistorySlot.key_range(hours)
        for key in keys:
            data = self.get_store(key)
            self.log.info("Reporting health key {} found {}".format(
                key, bool(data)))
            health = json.loads(data) if data else {}
            slot = health_util.HealthHistorySlot(health)
            collector.merge(slot)

        # include history that hasn't yet been flushed
        collector.merge(self._health_slot)

        return dict(
           current = json.loads(self.get("health")["json"]),
           history = collector.health()
        )

    def _version_parse(self, version):
        """
        Return the components of a Ceph version string.

        This returns nothing when the version string cannot be parsed into its
        constituent components, such as when Ceph has been built with
        ENABLE_GIT_VERSION=OFF.
        """
        r = "ceph version (?P<release>\d+)\.(?P<major>\d+)\.(?P<minor>\d+)"
        m = re.match(r, version)
        ver = {} if not m else {
            "release": m.group("release"),
            "major": m.group("major"),
            "minor": m.group("minor")
        }
        return { k:int(v) for k,v in six.iteritems(ver) }

    def _crash_history(self, hours):
        """
        Load crash history for the past N hours from the crash module.
        """
        params = dict(
            prefix = "crash json_report",
            hours = hours
        )

        result = dict(
            summary = {},
            hours = params["hours"],
        )

        health_check_details = []

        try:
            _, _, crashes = self.remote("crash", "handle_command", "", params)
            result["summary"] = json.loads(crashes)
        except Exception as e:
            errmsg = "failed to invoke crash module"
            self.log.warning("{}: {}".format(errmsg, str(e)))
            health_check_details.append(errmsg)
        else:
            self.log.debug("Crash module invocation succeeded {}".format(
                json.dumps(result["summary"], indent=2)))

        return result, health_check_details

    def _config_dump(self):
        """Report cluster configuration

        This report is the standard `config dump` report. It does not include
        configuration defaults; these can be inferred from the version number.
        """
        result = CommandResult("")
        args = dict(prefix = "config dump", format = "json")
        self.send_command(result, "mon", "", json.dumps(args), "")
        ret, outb, outs = result.wait()
        if ret == 0:
            return json.loads(outb), []
        else:
            self.log.warning("send_command 'config dump' failed. \
                    ret={}, outs=\"{}\"".format(ret, outs))
            return [], ["Failed to read monitor config dump"]

    def do_report(self, inbuf, command):
        health_check_details = []
        report = {}

        report.update({
            "version": dict(full = self.version,
                **self._version_parse(self.version))
        })

        # crash history
        crashes, health_details = self._crash_history(CRASH_HISTORY_HOURS)
        report["crashes"] = crashes
        health_check_details.extend(health_details)

        # health history
        report["health"] = self._health_report(HEALTH_HISTORY_HOURS)

        # cluster configuration
        config, health_details = self._config_dump()
        report["config"] = config
        health_check_details.extend(health_details)

        osd_map = self.get("osd_map")
        del osd_map['pg_temp']
        report["osd_dump"] = osd_map

        report["df"] = self.get("df")
        report["osd_tree"] = self.get("osd_map_tree")
        report["fs_map"] = self.get("fs_map")
        report["crush_map"] = self.get("osd_map_crush")
        report["mon_map"] = self.get("mon_map")
        report["service_map"] = self.get("service_map")
        report["manager_map"] = self.get("mgr_map")
        report["mon_status"] = json.loads(self.get("mon_status")["json"])
        report["pg_summary"] = self.get("pg_summary")
        report["osd_metadata"] = self.get("osd_metadata")

        report.update({
            "errors": health_check_details
        })

        if health_check_details:
            self.set_health_checks({
                INSIGHTS_HEALTH_CHECK: {
                    "severity": "warning",
                    "summary": "Generated incomplete Insights report",
                    "detail": health_check_details
                }
            })

        return 0, json.dumps(report, indent=2, cls=health_util.HealthEncoder), ""

    def do_prune_health(self, inbuf, command):
        try:
            hours = int(command['hours'])
        except ValueError:
            return errno.EINVAL, '', 'hours argument must be integer'

        self._health_prune_history(hours)

        return 0, "", ""

    def testing_set_now_time_offset(self, hours):
        """
        Control what "now" time it is by applying an offset. This is called from
        the selftest module to manage testing scenarios related to tracking
        health history.
        """
        hours = long(hours)
        health_util.NOW_OFFSET = datetime.timedelta(hours = hours)
        self.log.warning("Setting now time offset {}".format(health_util.NOW_OFFSET))

    def handle_command(self, inbuf, command):
        if command["prefix"] == "insights":
            return self.do_report(inbuf, command)
        elif command["prefix"] == "insights prune-health":
            return self.do_prune_health(inbuf, command)
        else:
            raise NotImplementedError(cmd["prefix"])