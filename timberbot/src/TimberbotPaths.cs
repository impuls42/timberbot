using System;
using System.IO;
using System.Runtime.InteropServices;

namespace Timberbot
{
    static class TimberbotPaths
    {
        public static bool IsWindows => RuntimeInformation.IsOSPlatform(OSPlatform.Windows);
        public static bool IsMacOS => RuntimeInformation.IsOSPlatform(OSPlatform.OSX);

        public static string TimberbornDocumentsDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "Timberborn");

        public static string ModDir =>
            Path.Combine(TimberbornDocumentsDir, "Mods", "Timberbot");

        public static string SettingsPath =>
            Path.Combine(ModDir, "settings.json");

        // Persisted widget/connector state introduced in the mod ↔ connector
        // architecture rework. Holds mode, goal, lastError between sessions.
        // See TimberbotAgentState for the on-disk schema.
        public static string StatePath =>
            Path.Combine(ModDir, "state.json");
    }
}
