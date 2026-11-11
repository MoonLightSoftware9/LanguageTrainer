"""Reject packages that need non-Windows DLLs outside the portable directory."""
from pathlib import Path
import sys

from package_progress import runtime_excluded

WINDOWS_DLLS = set('''msi icuuc bthprops fontsub uiautomationcore odbc32 advapi32 avrt authz bcrypt bcryptprimitives cabinet cfgmgr32 clbcatq combase comctl32 comdlg32 credui crypt32 cryptbase cryptsp cryptui d2d1 d3d9 d3d10 d3d10_1 d3d11 d3d12 d3dcompiler_47 dcomp dbgcore dbghelp dhcpcsvc dhcpcsvc6 dnsapi dsound dwmapi dwrite dxcore dxgi dxva2 evr faultrep fwpuclnt gdi32 gdi32full glu32 hid httpapi imagehlp imm32 iphlpapi kernel32 kernelbase magnification mf mfcore mfplat mfreadwrite mfuuid mmdevapi mpr msacm32 msasn1 msctf msdmo msimg32 msvcrt ncrypt netapi32 netutils normaliz ntdll ntdsapi ntmarta ole32 oleacc oleaut32 opengl32 pdh powrprof propsys psapi rasapi32 rasman rpcrt4 secur32 sechost sensapi setupapi shell32 shcore shlwapi srvcli sspicli tdh ucrtbase urlmon user32 userenv usp10 uxtheme version wevtapi windowscodecs win32u winhttp wininet winmm winnsi winscard winspool wintrust winusb wlanapi wldap32 wmcodecdspuuid wmvcore ws2_32 wtsapi32 wkscli'''.split())


def windows_dependency(name):
    stem = name.lower().rsplit('.', 1)[0]
    return stem in WINDOWS_DLLS or name.lower().startswith(('api-ms-win-', 'ext-ms-win-'))


def audit(directory):
    import pefile
    root = Path(directory).resolve()
    binaries = [p for p in root.rglob('*') if p.suffix.lower() in {'.dll', '.pyd', '.exe'} and not runtime_excluded(p.relative_to(root))]
    available = {p.name.lower() for p in binaries}
    missing = set()
    for path in binaries:
        try:
            image = pefile.PE(str(path), fast_load=True)
            image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'], pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
            for entry in getattr(image, 'DIRECTORY_ENTRY_IMPORT', []) + getattr(image, 'DIRECTORY_ENTRY_DELAY_IMPORT', []):
                name = entry.dll.decode('ascii').lower()
                if name not in available and not windows_dependency(name):
                    missing.add((str(path.relative_to(root)), name))
            image.close()
        except pefile.PEFormatError as error:
            raise RuntimeError(f'Invalid native binary: {path}') from error
    if missing:
        raise RuntimeError('Unbundled DLL dependencies:\n' + '\n'.join(f'{file}: {name}' for file, name in sorted(missing)))
    qt = root / 'Lib/site-packages/PySide6'
    for path in ('QtWebEngineProcess.exe', 'Qt6WebEngineCore.dll', 'resources/icudtl.dat', 'resources/qtwebengine_resources.pak', 'translations/qtwebengine_locales/en-US.pak', 'plugins/platforms/qwindows.dll', 'plugins/platforms/qoffscreen.dll'):
        if not (qt / path).is_file():
            raise RuntimeError('Missing bundled browser file: ' + str(qt / path))
    print(f'Runtime audit OK: {len(binaries)} native files; dependencies are bundled or supplied by Windows.', flush=True)
    return len(binaries)


if __name__ == '__main__':
    audit(sys.argv[1])
