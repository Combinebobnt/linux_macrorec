# macrorec Wayland/KWin spike - instructions

Thanks for running this. It's a diagnostic for a Linux macro-recording tool
(`linux_macrorec`, https://github.com/Combinebobnt/linux_macrorec) - it answers whether the
tool's approach could work on your Wayland/KDE desktop at all. It does **not** install or change
the tool itself.

## What it does

- Creates a couple of small virtual keyboard/mouse devices (via the kernel's `uinput`), sends a
  handful of harmless test inputs (a letter key, a click, a scroll tick, some mouse moves) into
  its own on-screen window, and checks whether they arrived.
- Reads your real keyboard for about 8 seconds at one point, to prove it can see real keystrokes -
  you'll be prompted to type a short phrase.
- Destroys every virtual device it creates when it's done (even if it crashes or is interrupted).
- Never touches your real keyboard or mouse device, only the virtual ones it creates itself.
- Injected keys are letters only - never Escape, Super, Alt, Ctrl, or anything that could trigger
  a shortcut.

## Before you run it (one-time setup)

```
python3 wayland_kwin_spike.py --setup
```

This prints a copy-paste block for your distro: it loads the `uinput` kernel module, adds a udev
rule, adds your user to the `input` group, and lists the packages to install
(`python3-evdev`, PyQt5, and the Qt Wayland platform plugin package).

**After running that block, log out and log back in.** A new terminal or `newgrp` is not enough -
group membership only takes effect on your next login.

## Running it

```
python3 wayland_kwin_spike.py
```

A window will cover your screen. **Click inside it** to start - that's what tells the script your
keyboard/mouse focus is on the test window rather than somewhere else. Don't touch the mouse or
keyboard again until it says to (it will prompt you to type a short phrase partway through).

The whole thing takes under a minute. It prints a `VERDICT` block at the end, several dozen lines
starting with `======`.

**Please paste that whole VERDICT block back**, exactly as printed.

## If something goes wrong

Every stage in the script is designed to fail gracefully and print what to do next, right there
in the output - so if you see a "SKIPPED" or an error message with a suggested fix, that's
expected, not a bug. Just include it in what you paste back.

If a window is left on screen and won't close, Alt+Tab to it and close it normally - no data or
settings are at risk.

## Undoing the setup afterwards

Once you're done, you can remove the group membership and udev rule `--setup` printed. Run
`python3 wayland_kwin_spike.py --setup` again - the same output includes the undo commands - and
log out and back in again afterwards for the group change to take effect.
