# Maintainer: Local Package
pkgname=bresun-microscope
pkgver=0.1.0
pkgrel=1
pkgdesc="Live 4K60 viewer, hardware-accelerated recorder, PCB measurement & stitching for Bresun HH-4K MAX"
arch=('any')
license=('MIT')
depends=('python' 'python-numpy' 'python-opencv' 'pyside6' 'ffmpeg' 'v4l-utils')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
source=()

build() {
    cd "$startdir"
    python -m build --wheel --no-isolation
}

package() {
    cd "$startdir"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 microscope.desktop "$pkgdir/usr/share/applications/microscope.desktop"
}
