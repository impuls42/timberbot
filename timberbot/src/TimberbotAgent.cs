// TimberbotAgent.cs. Launches an interactive agent session for the player.
//
// PR 2 of the mod-distribution rework moved all agent orchestration into the
// `tbot` Python package: prompt loading, instructions merging, platform
// terminal wrapping, and the actual agent binary invocation. This file is now
// a thin in-process wrapper that:
//   1. Validates the binary against the allowlist.
//   2. Spawns `tbot agent run --backend <binary> --goal "<goal>" ...` and waits.
//   3. Tracks status / lastError / cancellation for the in-game panel.
//
// The public API (Start / Stop / Status + properties) is preserved so
// `TimberbotPanel.cs` keeps working without changes.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

namespace Timberbot
{
    public enum AgentStatus
    {
        Idle,
        GatheringState,
        Interactive,
        Done,
        Error
    }

    public class TimberbotAgent
    {
        private readonly string _terminal;
        private readonly string _tbotCommand;
        private readonly bool _allowlistEnabled;
        private readonly HashSet<string> _allowedBinaries;

        private string _binary;
        private string _model;
        private string _effort;
        private string _goal;
        private string _commandTemplate;
        private string _terminalOverride;
        private int _processTimeoutSeconds;

        private const string DEFAULT_GOAL = "reach 50 beavers with 77 well-being";
        private const string DEFAULT_TBOT_COMMAND = "tbot";

        private AgentStatus _status = AgentStatus.Idle;
        private string _lastError;
        private string _currentCmd;
        private Thread _thread;
        private volatile bool _cancelRequested;
        private volatile Process _activeProcess;

        // `terminal` and `pythonCommand` are kept as constructor parameters for
        // backward compatibility with `TimberbotService.Load()`. PR 4 trims the
        // settings.json schema and removes both. Today `pythonCommand` is
        // ignored entirely (Python lives behind the `tbot` console script);
        // `terminal` is forwarded to `tbot agent run --terminal-prefix` so the
        // existing Windows Terminal UX keeps working.
        public TimberbotAgent(
            string terminal,
            string pythonCommand,
            bool allowlistEnabled = true,
            HashSet<string> allowedBinaries = null,
            string tbotCommand = null)
        {
            _terminal = terminal ?? "";
            _tbotCommand = string.IsNullOrWhiteSpace(tbotCommand) ? DEFAULT_TBOT_COMMAND : tbotCommand;
            _allowlistEnabled = allowlistEnabled;
            _allowedBinaries = allowedBinaries;
        }

        public AgentStatus CurrentStatus => _status;
        public string CurrentGoal => _goal;
        public string CurrentCommand => _currentCmd;
        public string LastError => _lastError;
        public string Binary => _binary;
        public string Effort => _effort;

        private readonly TimberbotJw _jw = new TimberbotJw(1024);
        private readonly TimberbotJw _statusJw = new TimberbotJw(4096);

        public string Start(string binary, string model, string effort, int timeout, string goal, string command = null, string terminal = null)
        {
            if (_status != AgentStatus.Idle && _status != AgentStatus.Done && _status != AgentStatus.Error)
                return _jw.Error("agent_busy", ("status", _status.ToString().ToLowerInvariant()));

            var resolvedBinary = string.IsNullOrWhiteSpace(binary) ? "claude" : binary;
            if (_allowlistEnabled && !TimberbotPure.IsAllowedBinary(resolvedBinary, _allowedBinaries))
                return _jw.Error("agent_binary_blocked: " + resolvedBinary + " is not in the allowlist. add it to agentAllowedBinaries in settings.json or set agentAllowlistEnabled=false");

            _binary = resolvedBinary;
            _model = model;
            _effort = effort;
            _commandTemplate = string.IsNullOrWhiteSpace(command) ? null : command;
            // terminal override from HTTP is ignored for security. only settings.json value is used.
            _terminalOverride = null;
            _processTimeoutSeconds = timeout > 0 ? timeout : 120;
            _goal = string.IsNullOrEmpty(goal) ? DEFAULT_GOAL : goal;
            _lastError = null;
            _currentCmd = null;
            _cancelRequested = false;
            _status = AgentStatus.GatheringState;

            _thread = new Thread(InteractiveSession) { IsBackground = true, Name = "Timberbot-Agent" };
            _thread.Start();

            TimberbotLog.Info($"agent.start binary={_binary} model={_model ?? "default"} custom={_commandTemplate != null}");
            return _jw.Reset().OpenObj()
                .Prop("status", "started")
                .Prop("binary", _binary)
                .CloseObj().ToString();
        }

        public string Stop()
        {
            if (_status == AgentStatus.Idle || _status == AgentStatus.Done || _status == AgentStatus.Error)
                return _jw.Error("agent_not_running");

            _cancelRequested = true;
            try
            {
                if (_activeProcess != null && !_activeProcess.HasExited)
                    _activeProcess.Kill();
            }
            catch (Exception ex)
            {
                TimberbotLog.Info($"agent.stop.kill.fail: {ex.Message}");
            }

            TimberbotLog.Info("agent.stop requested");
            return _jw.Reset().OpenObj().Prop("status", "stopping").CloseObj().ToString();
        }

        public string Status()
        {
            return _statusJw.Reset().OpenObj()
                .Prop("status", _status.ToString().ToLowerInvariant())
                .Prop("binary", _binary ?? "")
                .Prop("model", _model ?? "")
                .Prop("goal", JsonEscape(_goal))
                .Prop("currentCmd", JsonEscape(_currentCmd))
                .Prop("lastError", JsonEscape(_lastError))
                .CloseObj().ToString();
        }

        private static string JsonEscape(string s) => TimberbotPure.JsonEscape(s);

        private void InteractiveSession()
        {
            try
            {
                _status = AgentStatus.GatheringState;

                var effectiveTerminal = _terminalOverride ?? _terminal;
                var args = TimberbotPure.BuildTbotAgentRunArgs(
                    _binary, _goal, _model, _effort, _commandTemplate, effectiveTerminal);
                _currentCmd = $"{_tbotCommand} {args}";

                TimberbotLog.Info($"agent.launch cmd={_tbotCommand} args={args}");

                var psi = new ProcessStartInfo
                {
                    FileName = _tbotCommand,
                    Arguments = args,
                    UseShellExecute = true,
                    WorkingDirectory = TimberbotPaths.ModDir,
                };

                _status = AgentStatus.Interactive;

                using var proc = new Process { StartInfo = psi };
                _activeProcess = proc;
                proc.Start();
                proc.WaitForExit();
                _activeProcess = null;

                TimberbotLog.Info($"agent.done exitCode={proc.ExitCode} cancelled={_cancelRequested}");
                _status = _cancelRequested ? AgentStatus.Idle : AgentStatus.Done;
            }
            catch (Exception ex)
            {
                _lastError = ex.Message;
                _status = AgentStatus.Error;
                TimberbotLog.Error("agent.interactive", ex);
            }
            finally
            {
                _activeProcess = null;
            }
        }
    }
}
