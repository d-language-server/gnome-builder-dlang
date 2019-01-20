import gi
import os

from gi.repository import GLib
from gi.repository import Gio
from gi.repository import GObject
from gi.repository import Ide

_ = Ide.gettext

_ERROR_FORMAT_REGEX = ("^(?<filename>.+\\.di?)\\D"
                        "(?<line>\\d+)(?:,|:)?"
                        "(?<column>\\d+)?\\S+\\s+"
                        "(?<level>\\w+):\\s+"
                        "(?<message>.+)$")


def get_working_dir(context):
    return context.get_vcs().get_working_directory()


class DubBuildSystem(Ide.Object, Ide.BuildSystem, Gio.AsyncInitable):
    project_file = GObject.Property(type=Gio.File)

    def do_get_id(self):
        return "dub"

    def do_get_display_name(self):
        return "Dub"

    def do_init_async(self, io_priority, cancellable, callback, data):
        task = Gio.Task.new(self, cancellable, callback)

        try:
            if self.props.project_file.get_basename() in ("dub.json", "dub.sdl"):
                task.return_boolean(True)
                return

            if self.props.project_file.query_file_type(0) == Gio.FileType.DIRECTORY:
                if self.props.project_file.get_child("dub.json").query_exists(None) \
                        or self.props.project_file.get_child("dub.sdl").query_exists(None):
                    task.return_boolean(True)
                    return
        except Exception as ex:
            task.return_error(ex)

        task.return_error(Ide.NotSupportedError())

    def do_init_finish(self, task):
        return task.propagate_boolean()


class DubPipelineAddin(Ide.Object, Ide.BuildPipelineAddin):
    def do_load(self, pipeline):
        context = self.get_context()
        build_system = context.get_build_system()

        self.error_format_id = pipeline.add_error_format(_ERROR_FORMAT_REGEX,
            GLib.RegexCompileFlags.OPTIMIZE |
            GLib.RegexCompileFlags.CASELESS)

        if type(build_system) != DubBuildSystem:
            return

        config = pipeline.get_configuration()
        workdir_path = get_working_dir(context).get_path()

        fetch_launcher = pipeline.create_launcher()
        fetch_launcher.push_argv("dub")
        fetch_launcher.push_argv("--root=" + workdir_path)
        fetch_launcher.push_argv("upgrade")
        fetch_launcher.push_argv("--missing-only")
        self.track(pipeline.connect_launcher(Ide.BuildPhase.DOWNLOADS, 0, fetch_launcher))

        build_launcher = pipeline.create_launcher()
        build_launcher.push_argv("dub")
        build_launcher.push_argv("--root=" + workdir_path)
        build_launcher.push_argv("build")
        build_launcher.push_argv("--build=" + ("debug" if config.props.debug else "release"))

        clean_launcher = pipeline.create_launcher()
        clean_launcher.push_argv("dub")
        clean_launcher.push_argv("--root=" + workdir_path)
        clean_launcher.push_argv("clean")

        build_stage = Ide.BuildStageLauncher.new(context, build_launcher)
        build_stage.set_name(_("Building project"))
        build_stage.set_clean_launcher(clean_launcher)
        build_stage.connect("query", self._query)
        self.track(pipeline.connect(Ide.BuildPhase.BUILD, 0, build_stage))

    def do_unload(self, pipeline):
        if self.error_format_id:
            pipeline.remove_error_format(self.error_format_id)

    def _query(self, stage, pipeline, cancellable):
        stage.set_completed(False)


class DubBuildTarget(Ide.Object, Ide.BuildTarget):
    def do_get_install_directory(self):
        return None

    def do_get_name(self):
        return "dub-run"

    def do_get_language(self):
        return "d"

    def do_get_argv(self):
        return ["dub", "--root=" + get_working_dir(self.get_context()).get_path(), "run"]


class DubBuildTargetProvider(Ide.Object, Ide.BuildTargetProvider):

    def do_get_targets_async(self, cancellable, callback, data):
        task = Gio.Task.new(self, cancellable, callback)
        task.set_priority(GLib.PRIORITY_LOW)

        context = self.get_context()
        build_system = context.get_build_system()

        if type(build_system) != DubBuildSystem:
            task.return_error(GLib.Error('Not dub build system',
                domain=GLib.quark_to_string(Gio.io_error_quark()),
                code=Gio.IOErrorEnum.NOT_SUPPORTED))
            return

        task.targets = [DubBuildTarget(context=context)]
        task.return_boolean(True)

    def do_get_targets_finish(self, result):
        if result.propagate_boolean():
            return result.targets


class DubDependencyUpdater(Ide.Object, Ide.DependencyUpdater):

    def do_update_async(self, cancellable, callback, data):
        task = Gio.Task.new(self, cancellable, callback)
        task.set_priority(GLib.PRIORITY_LOW)

        context = self.get_context()
        build_system = context.get_build_system()

        if type(build_system) != DubBuildSystem:
            task.return_boolean(True)
            return

        build_manager = context.get_build_manager()
        pipeline = build_manager.get_pipeline()

        if not pipeline:
            task.return_error(GLib.Error('Cannot update dependencies without build pipeline',
                domain=GLib.quark_to_string(Gio.io_error_quark()),
                code=Gio.IOErrorEnum.FAILED))
            return

        launcher = pipeline.create_launcher()
        launcher.push_argv("dub")
        launcher.push_argv("--root=" + get_working_dir(context).get_path())
        launcher.push_argv("upgrade")

        try:
            subprocess = launcher.spawn()
            subprocess.wait_check_async(None, self.wait_check_cb, task)
        except Exception as ex:
            task.return_error(ex)

    def do_update_finish(self, result):
        return result.propagate_boolean()

    def wait_check_cb(self, subprocess, result, task):
        try:
            subprocess.wait_check_finish(result)
            task.return_boolean(True)
        except Exception as ex:
            task.return_error(ex)


class DlangService(Ide.Object, Ide.Service):
    _client = None
    _has_started = False
    _monitor = None

    @GObject.Property(type=Ide.LangservClient)
    def client(self):
        return self._client

    @client.setter
    def client(self, value):
        self._client = value
        self.notify("client")

    def do_context_loaded(self):
        context = self.get_context()
        project_file = context.get_project_file()
        project_dir = project_file if project_file.query_file_type(0, None) == Gio.FileType.DIRECTORY \
            else project_file.get_parent()
        selections_file = project_dir.get_child("dub.selections.json")
        self._monitor = selections_file.monitor(0, None)
        self._monitor.set_rate_limit(1000)
        self._monitor.connect("changed", self._monitor_changed_cb)

    def _monitor_changed_cb(self, monitor, file, other_file, event_type):
        if self._client and file.get_basename() == "dub.selections.json":
            file_event = GLib.Variant("a{sv}", { "uri": GLib.Variant.new_string(file.get_uri()), "type": GLib.Variant.new_byte(2) })
            files_params = GLib.Variant("a{sav}", { "changes": [file_event] })
            self._client.send_notification_async("workspace/didChangeWatchedFiles", files_params, None, self._dls_notification_finish, None)

    def do_stop(self):
        if self._monitor is not None:
            monitor, self._monitor = self._monitor, None
            monitor.cancel()

        if self._client is not None:
            client, self._client = self._client, None
            client.stop()

    def _ensure_started(self):
        if self._has_started:
            return

        self._has_started = True
        launcher = self._create_launcher()
        launcher.set_clear_env(False)

        launcher.set_cwd(get_working_dir(self.get_context()).get_path())
        launcher.push_argv(os.path.join(GLib.get_home_dir(), ".dub", "packages", ".bin", "dls-latest", "dls"))
        launcher.push_argv("--stdio")

        supervisor = Ide.SubprocessSupervisor()
        supervisor.connect("spawned", self._dls_spawned)
        supervisor.set_launcher(launcher)
        supervisor.start()

    def _dls_spawned(self, supervisor, subprocess):
        stdin = subprocess.get_stdin_pipe()
        stdout = subprocess.get_stdout_pipe()
        io_stream = Gio.SimpleIOStream.new(stdout, stdin)

        if self._client:
            self._client.stop()

        settings = GLib.Variant("a{sv}", { "symbol": GLib.Variant("a{sb}", { "listLocalSymbols": True }) })
        dls_settings = GLib.Variant("a{sv}", { "d": GLib.Variant("a{sv}", { "dls": settings }) })
        config_params = GLib.Variant("a{sv}", { "settings": dls_settings })

        self._client = Ide.LangservClient.new(self.get_context(), io_stream)
        self._client.add_language("d")
        self._client.start()
        self._client.send_notification_async("initialized", None, None, self._dls_notification_finish, None)
        self._client.send_notification_async("workspace/didChangeConfiguration", config_params, None, self._dls_notification_finish, None)
        self.notify("client")

    def _dls_notification_finish(self, source_object, result, user_data):
        if self._client:
            self._client.send_notification_finish(result)

    def _create_launcher(self):
        flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
        launcher = Ide.SubprocessLauncher()
        launcher.set_flags(flags)
        launcher.set_cwd(GLib.get_home_dir())
        launcher.set_run_on_host(True)
        return launcher

    @classmethod
    def bind_client(klass, provider):
        self = provider.get_context().get_service_typed(DlangService)
        self._ensure_started()
        self.bind_property("client", provider, "client", GObject.BindingFlags.SYNC_CREATE)

try:
    class DlangDiagnosticProvider(Ide.LangservDiagnosticProvider):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangCompletionProvider(Ide.LangservCompletionProvider):
        def do_load(self, context):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangRenameProvider(Ide.LangservRenameProvider):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangSymbolResolver(Ide.LangservSymbolResolver):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangHighlighter(Ide.LangservHighlighter):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangFormatter(Ide.LangservFormatter):
        def do_load(self):
            DlangService.bind_client(self)
except AttributeError: pass

try:
    class DlangHoverProvider(Ide.LangservHoverProvider):
        def do_prepare(self):
            self.props.category = "D"

        def do_load(self, context):
            DlangService.bind_client(self)
except AttributeError: pass
