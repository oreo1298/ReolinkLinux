# Maintainer: oreo1298
#
# Arch Linux and Arch-based distributions (CachyOS, EndeavourOS, Manjaro, Garuda, ...).
# Builds the working tree this file sits in:
#
#   git clone https://github.com/oreo1298/ReolinkLinux.git
#   cd ReolinkLinux
#   makepkg -si
#
# To update later: `git pull` then `makepkg -sif`.

pkgname=reolinklinux
pkgver=1.0.3
pkgrel=1
pkgdesc="Modern client for Reolink PoE cameras and NVRs: full-quality live view, PTZ, playback, recording"
arch=('any')
url="https://github.com/oreo1298/ReolinkLinux"
license=('MIT')
depends=('python' 'pyside6' 'qt6-svg' 'mpv' 'ffmpeg')
optdepends=('python-keyring: keep camera passwords in GNOME Keyring / KWallet'
            'libva-intel-driver: hardware video decoding on older Intel GPUs'
            'intel-media-driver: hardware video decoding on newer Intel GPUs'
            'libva-mesa-driver: hardware video decoding on AMD GPUs')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')

_srcdir="$startdir"

build() {
  cd "$_srcdir"
  rm -rf dist build
  python -m build --wheel --no-isolation
}

check() {
  cd "$_srcdir"
  QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
}

package() {
  cd "$_srcdir"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 data/io.github.oreo1298.ReolinkLinux.desktop \
    "$pkgdir/usr/share/applications/io.github.oreo1298.ReolinkLinux.desktop"
  install -Dm644 data/io.github.oreo1298.ReolinkLinux.metainfo.xml \
    "$pkgdir/usr/share/metainfo/io.github.oreo1298.ReolinkLinux.metainfo.xml"
  install -Dm644 reolinklinux/data/reolinklinux.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.oreo1298.ReolinkLinux.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 NOTICE "$pkgdir/usr/share/licenses/$pkgname/NOTICE"
}
