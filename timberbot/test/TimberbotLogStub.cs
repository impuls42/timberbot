// TimberbotLogStub.cs — test-side stand-in for the Unity TimberbotLog class.
//
// The production logger writes via UnityEngine.Debug and a session log file
// in the mod folder. Neither is available in the xUnit test assembly, so we
// supply a header-compatible stub with the same surface used by shared code
// (TimberbotWebSocketServer in particular). All methods are no-ops; tests
// don't assert on log lines.

using System;

namespace Timberbot
{
    internal static class TimberbotLog
    {
        public static void Info(string _msg) { }
        public static void Error(string _phase, Exception _ex) { }
        public static void Error(string _msg, params object[] _args) { }
    }
}
