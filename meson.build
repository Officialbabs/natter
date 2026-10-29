# Installs Natter the way distribution packages and the Flatpak (packaging/) want it:
#
#   meson setup build --prefix=/usr && meson install -C build
#
# The natter package goes to <datadir>/natter, outside Python's site-packages, so it doesn't
# depend on the Python version; bin/natter is installed with that folder written into it.
# ./install.sh is the other way in: it runs Natter from a checkout, for one user.

project('natter',
  version: '0.1.8',  # keep in step with VERSION in natter/__init__.py and the metainfo's release
  license: 'GPL-3.0-or-later',
  meson_version: '>= 0.62',
)

app_id = 'io.github.Officialbabs.Natter'
python = import('python').find_installation('python3')
prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
datadir = prefix / get_option('datadir')
pkgdatadir = datadir / meson.project_name()

install_subdir('natter',
  install_dir: pkgdatadir,
  exclude_directories: ['__pycache__', 'camfix'],  # camfix: C source, built below
)

# The camera fix (natter/camfix/nattercamfix.c, used by natter/gstfix.py): a GStreamer tracer that
# stops WebKit's camera freezing on its first frame with GStreamer 1.28 and newer, and its Web Audio
# clicking after silence. Optional: built when there's a C compiler and GStreamer's development
# files; Natter runs without it. gstfix.py finds it through the camfix-dir file installed next to it.
camfix_dir = prefix / get_option('libdir') / meson.project_name() / 'gstreamer-1.0'
gstreamer = dependency('', required: false)
if add_languages('c', required: false, native: false)
  gstreamer = dependency('gstreamer-1.0', required: false)
endif
if gstreamer.found()
  shared_module('nattercamfix', 'natter' / 'camfix' / 'nattercamfix.c',
    dependencies: gstreamer,
    c_args: ['-DNATTER_VERSION="' + meson.project_version() + '"'],
    install: true,
    install_dir: camfix_dir,
  )
  configure_file(
    input: 'natter' / 'camfix' / 'camfix-dir.in',
    output: 'camfix-dir',
    configuration: {'DIR': camfix_dir},
    install: true,
    install_dir: pkgdatadir / 'natter',
  )
endif

# The launcher: the Python that has PyGObject on its first line, the package's folder in NATTER_DIR.
configure_file(
  input: 'bin/natter',
  output: 'natter',
  command: [find_program('sed'),
            '-e', '1s|.*|#!' + python.full_path() + '|',
            '-e', 's|^NATTER_DIR = "".*|NATTER_DIR = "' + pkgdatadir + '"|',
            '@INPUT@'],
  capture: true,
  install: true,
  install_dir: bindir,
  install_mode: 'rwxr-xr-x',
)

desktop_file = configure_file(
  input: 'natter.desktop.in',
  output: app_id + '.desktop',
  configuration: {'EXEC': 'natter'},
  install: true,
  install_dir: datadir / 'applications',
)

install_subdir('data/icons', install_dir: datadir)

metainfo = files('data' / app_id + '.metainfo.xml')
install_data(metainfo, install_dir: datadir / 'metainfo')

# Desktop search: the provider file, and a D-Bus service so the bus name has something behind it.
install_data('data' / app_id + '.search-provider.ini', install_dir: datadir / 'gnome-shell' / 'search-providers')
configure_file(
  input: 'data' / app_id + '.service.in',
  output: app_id + '.service',
  configuration: {'EXEC': bindir / 'natter'},
  install: true,
  install_dir: datadir / 'dbus-1' / 'services',
)

# Byte-compile the package where it's installed, so Natter doesn't compile itself on every start.
# Packages built with DESTDIR leave this to their own tooling (dh_python3, brp-python-bytecompile…).
meson.add_install_script(python, '-m', 'compileall', '-q', pkgdatadir, skip_if_destdir: true)

desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test('desktop file', desktop_file_validate, args: [desktop_file])
endif
appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('metainfo', appstreamcli, args: ['validate', '--no-net', metainfo])
endif
test('unit tests', python,
  args: ['-m', 'unittest', 'discover', '-s', meson.project_source_root() / 'tests'],
  workdir: meson.project_source_root(),
)
