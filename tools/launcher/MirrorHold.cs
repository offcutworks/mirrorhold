// The file a user double-clicks in a release: opens MirrorHold's window.
//
// A release's interpreter is CPython's own signed pythonw.exe in runtime\, started as
// "pythonw.exe -m mirrorhold_gui" in the release's folder, exactly as the Start menu entry
// and the scheduled tasks start it. This launcher only starts that process, passing on
// any arguments it was given, and exits: it carries the program's name and icon, which
// the interpreter cannot carry without losing its signature.
//
// The release's folder is the one this file was started from, as that path names it: a
// folder reached through a junction (a package manager's "current") stays named so, which
// is what lets the copy set up there keep working when the package manager points the
// junction at a newer release. Only where that folder holds no interpreter — this file
// started through a symbolic link to it, as a package manager's command alias does, which
// puts the link's own folder there — is the file the link leads to found, and its folder
// taken instead.
//
// A release works from a folder whose path is at most LongestFolder characters long. From
// a longer one its interpreter is started by the folder's 8.3 name, where the drive keeps
// one that short; where it keeps none, the launcher says why and stops. Every path is
// asked of Windows' own wide calls, never of the .NET Framework's path checks, which refuse
// a path past 260 characters and stop the program on an assertion dialog for an 8.3 name
// that expands past it (measured on the development machine).
//
// build_release.py compiles it with the .NET Framework compiler every Windows 10 and 11
// carries, adding the icon and the assembly attributes that name the release.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32.SafeHandles;

static class Launcher
{
    const string Title = "MirrorHold";

    // The longest path of a folder a release works from as it is named: pywin32 loads its
    // shell extensions from runtime\Lib\site-packages\win32comext, 38 characters below the
    // folder, only while that path is shorter than 248 characters. From a folder of 234
    // characters Windows refuses to load the interpreter, and from 240 to start it by its
    // path (measured on the development machine).
    const int LongestFolder = 209;

    // Names a file to write what stops the launcher to, instead of showing it in a dialog
    // that waits for a person: what a test reads.
    const string MessageFileVariable = "MIRRORHOLD_LAUNCHER_MESSAGE_FILE";

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security,
                                             uint disposition, uint flags, IntPtr template);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle file, StringBuilder path, uint size, uint flags);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFileAttributesW(string name);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetModuleFileNameW(IntPtr module, StringBuilder name, uint size);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetShortPathNameW(string longPath, StringBuilder shortPath, uint size);

    const int LongestPath = 32767;   // what the wide calls take with the \\?\ prefix

    static string Interpreter(string folder)
    {
        return folder + @"\runtime\pythonw.exe";
    }

    // "path" as the wide calls take it whatever its length.
    static string Extended(string path)
    {
        if (path.StartsWith(@"\\?\"))
            return path;
        return path.StartsWith(@"\\") ? @"\\?\UNC\" + path.Substring(2) : @"\\?\" + path;
    }

    static string Plain(string path)
    {
        if (path.StartsWith(@"\\?\UNC\"))
            return @"\\" + path.Substring(8);
        return path.StartsWith(@"\\?\") ? path.Substring(4) : path;
    }

    static bool IsFile(string path)
    {
        const uint Invalid = 0xFFFFFFFF, Directory = 0x10;
        uint attributes = GetFileAttributesW(Extended(path));
        return attributes != Invalid && (attributes & Directory) == 0;
    }

    // The folder "path" lies in; null for a path that names none.
    static string Parent(string path)
    {
        int last = path.LastIndexOf('\\');
        return last > 0 ? path.Substring(0, last) : null;
    }

    // "folder" as a working directory: a drive's root keeps its backslash, without which
    // "E:" names the drive's current folder rather than its root.
    static string AsDirectory(string folder)
    {
        return folder.EndsWith(":") ? folder + @"\" : folder;
    }

    // The path this program was started by; null when it cannot be read.
    static string ModulePath()
    {
        var text = new StringBuilder(LongestPath);
        uint length = GetModuleFileNameW(IntPtr.Zero, text, (uint)text.Capacity);
        return length == 0 || length >= text.Capacity ? null : text.ToString();
    }

    // The path a file opened by "path" really has, every link on the way followed; null when
    // it cannot be read.
    static string FinalPath(string path)
    {
        const uint ShareAll = 1 | 2 | 4, OpenExisting = 3, BackupSemantics = 0x02000000;
        using (SafeFileHandle file = CreateFileW(Extended(path), 0, ShareAll, IntPtr.Zero, OpenExisting,
                                                 BackupSemantics, IntPtr.Zero))
        {
            if (file.IsInvalid)
                return null;
            var text = new StringBuilder(260);
            uint length = GetFinalPathNameByHandleW(file, text, (uint)text.Capacity, 0);
            if (length >= text.Capacity)   // too short: the answer is the size the path needs
            {
                text = new StringBuilder((int)length + 1);
                length = GetFinalPathNameByHandleW(file, text, (uint)text.Capacity, 0);
            }
            if (length == 0 || length >= text.Capacity)
                return null;
            return Plain(text.ToString());
        }
    }

    static string ReleaseFolder()
    {
        string started = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        if (IsFile(Interpreter(started)))
            return started;
        string module = ModulePath();
        string target = module == null ? null : FinalPath(module);
        string folder = target == null ? null : Parent(target);
        return folder != null && IsFile(Interpreter(folder)) ? folder : started;
    }

    // "folder" named so that Windows starts the release from it: as it is, or by its 8.3
    // name where it is too long and its drive keeps one short enough; null where neither.
    static string Startable(string folder)
    {
        if (folder.Length <= LongestFolder)
            return folder;
        var text = new StringBuilder(LongestPath);
        uint length = GetShortPathNameW(Extended(folder), text, (uint)text.Capacity);
        if (length == 0 || length >= text.Capacity)
            return null;
        string shortName = Plain(text.ToString());
        return shortName.Length <= LongestFolder ? shortName : null;
    }

    static void Say(string text)
    {
        string file = Environment.GetEnvironmentVariable(MessageFileVariable);
        if (!string.IsNullOrEmpty(file))
        {
            try
            {
                File.WriteAllText(file, text);
                return;
            }
            catch (Exception)   // a file that cannot be written: the dialog says it instead
            {
            }
        }
        MessageBox.Show(text, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    // One argument as the C runtime reads it back from a command line.
    static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
            return arg;
        var text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                slashes++;
                continue;
            }
            if (c == '"')
            {
                text.Append('\\', slashes * 2 + 1).Append('"');
                slashes = 0;
                continue;
            }
            text.Append('\\', slashes).Append(c);
            slashes = 0;
        }
        text.Append('\\', slashes * 2);
        return text.Append('"').ToString();
    }

    [STAThread]
    static int Main(string[] args)
    {
        string folder = ReleaseFolder();
        string interpreter = Interpreter(folder);
        if (!IsFile(interpreter))
        {
            Say("This copy of MirrorHold is incomplete: " + interpreter + " is missing. Unpack the release again.");
            return 1;
        }
        string startable = Startable(folder);
        if (startable == null)
        {
            Say("MirrorHold cannot start from this folder: its path is " + folder.Length + " characters long, and "
                + "on this drive Windows can start MirrorHold only from a folder whose path is at most "
                + LongestFolder + " characters long. Move the folder to a shorter path, for example "
                + @"C:\MirrorHold, and start MirrorHold there.");
            return 1;
        }
        var line = new StringBuilder("-m mirrorhold_gui");
        foreach (string arg in args)
            line.Append(' ').Append(Quote(arg));
        var start = new ProcessStartInfo(Interpreter(startable), line.ToString());
        start.WorkingDirectory = AsDirectory(startable);
        start.UseShellExecute = false;
        try
        {
            Process.Start(start);
        }
        catch (Win32Exception exc)
        {
            Say("MirrorHold could not start: " + exc.Message);
            return 1;
        }
        return 0;
    }
}
