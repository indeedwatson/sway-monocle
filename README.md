Bring bspwm's monocle mode to sway, with a few tweaks:

- If there's only one window in the workspace, hide the titlebar.
- If there's more than one window, the script toggles between tabbed and the spiral binary tree from bspwm
- Additionally, with the option --right-toggle it only alternates between tabbed and tiled in the right-most split of the workspace

# Usage

Add this to your `~/.config/sway/config`:
```
    bindsym $mod+c exec python ~/.config/sway/monocle.py
    bindsym $mod+e exec python ~/.config/sway/monocle.py --right-toggle
```
