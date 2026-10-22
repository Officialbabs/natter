# natter-den-git on the AUR

`PKGBUILD` and `.SRCINFO` for the AUR package `natter-den-git`, which builds Natter Den from the
latest commit on GitHub with `meson.build`. Only these two files go to the AUR; this README stays
here.

It isn't on the AUR yet (the AUR isn't taking new accounts right now). Until it is, build it from
this folder, which needs the GitHub repository to be public (the PKGBUILD downloads the source):

```sh
makepkg -si
```

Once it's on the AUR, Arch users install it with:

```sh
yay -S natter-den-git          # or paru -S natter-den-git
# or by hand:
git clone https://aur.archlinux.org/natter-den-git.git
cd natter-den-git && makepkg -si
```

## Before the first upload

- **The GitHub repository must be public.** makepkg clones
  `https://github.com/Officialbabs/natter.git` without signing in; while the repository is
  private, every build stops at the download.
- The `# Maintainer:` line uses the GitHub no-reply address from the commits. Change it to an
  address you're happy to show on the AUR, if you want people to be able to email you.

## Publishing

1. Make an account on <https://aur.archlinux.org> and add your SSH public key in *My Account*.
2. Clone the (still empty) package and copy the two files in:

   ```sh
   git clone ssh://aur@aur.archlinux.org/natter-den-git.git aur-natter-den-git
   cp PKGBUILD .SRCINFO aur-natter-den-git/
   cd aur-natter-den-git
   git add PKGBUILD .SRCINFO
   git commit -m "Initial upload: natter-den-git r6.ab3e527"
   git push
   ```

   The first push creates the package; the AUR only accepts pushes to the `master` branch.

## Updating

- New commits on GitHub need no upload: a `-git` package builds whatever is on GitHub at
  install time, and AUR helpers rebuild it when asked to (`yay -Syu --devel`).
- When the PKGBUILD changes (new dependency, new file), bump `pkgrel` (or set `pkgver` to the
  latest `r<commits>.<hash>` and `pkgrel=1`), regenerate `.SRCINFO` on an Arch system, and push
  both:

  ```sh
  makepkg --printsrcinfo > .SRCINFO
  ```

- To test on Arch before pushing: `makepkg -si` in the AUR clone (not in this checkout, where
  makepkg would leave `src/`, `pkg/` and the package), or a clean chroot with
  `extra-x86_64-build` (devtools), then `namcap PKGBUILD` and `namcap natter-den-git-*.pkg.tar.zst`.

## What the package does

- `build()`: `arch-meson` and `meson compile`. That also builds the camera fix
  (`/usr/lib/natter/gstreamer-1.0/libnattercamfix.so`), which is why the package is
  `x86_64 aarch64` and not `any`.
- `check()`: `meson test` (desktop file, metainfo, unit tests) in a home folder of its own, so
  the tests don't make folders in yours or reach a Natter Den that's running.
- `package()`: `meson install`, then byte-compiles `/usr/share/natter`, which meson skips when
  installing into a package.
- Dependencies cover everything, calls inside the app included (`gst-plugins-bad` and `libnice`
  for GStreamer's WebRTC). `gst-libav` plays WhatsApp's H.264 videos and AAC sound.
