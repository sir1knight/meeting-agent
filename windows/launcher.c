// Meeting Agent.exe — запускалка для Windows: python\pythonw.exe -m meeting_agent winapp
// из папки рядом с собой, без окна консоли. Собирается на Mac: zig cc (build_windows.sh).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR args, int show) {
    (void)inst; (void)prev; (void)args; (void)show;
    wchar_t exe[MAX_PATH * 4], dir[MAX_PATH * 4], python[MAX_PATH * 4], app[MAX_PATH * 4], cmd[MAX_PATH * 8];
    DWORD n = GetModuleFileNameW(NULL, exe, (DWORD)(sizeof exe / sizeof *exe));
    if (n == 0 || n >= sizeof exe / sizeof *exe) return 1;
    wcscpy(dir, exe);
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    swprintf(python, sizeof python / sizeof *python, L"%ls\\python\\pythonw.exe", dir);
    swprintf(app, sizeof app / sizeof *app, L"%ls\\app", dir);
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"Не найдена папка python рядом с Meeting Agent.exe.\n\n"
                          L"Распакуйте архив целиком и запускайте программу из распакованной папки.",
                    L"Meeting Agent", MB_ICONERROR);
        return 1;
    }
    SetEnvironmentVariableW(L"PYTHONPATH", app);
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    SetEnvironmentVariableW(L"MEETING_AGENT_EXE", exe);
    swprintf(cmd, sizeof cmd / sizeof *cmd, L"\"%ls\" -m meeting_agent winapp", python);

    STARTUPINFOW si = {sizeof si};
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(python, cmd, NULL, NULL, FALSE, 0, NULL, app, &si, &pi)) {
        MessageBoxW(NULL, L"Не удалось запустить Meeting Agent (python\\pythonw.exe).", L"Meeting Agent", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
