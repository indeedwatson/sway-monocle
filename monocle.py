#!/usr/bin/env python3
"""Toggle the focused Sway workspace between global tabs and side-by-side splits.

Requires Sway and swaymsg. Uses only Python's standard library.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time


def sway(*args):
    result = subprocess.run(["swaymsg", "-r", *args], text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "swaymsg failed")
    return result.stdout


def subtree_has_focus(node):
    """Sway marks the focused leaf/container, not reliably the workspace."""
    if node.get("focused"):
        return True
    return any(
        subtree_has_focus(child)
        for child in node.get("nodes", []) + node.get("floating_nodes", [])
    )


def focused_workspace(node):
    # A workspace is focused when one of its descendants is focused.
    if node.get("type") == "workspace" and subtree_has_focus(node):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = focused_workspace(child)
        if found:
            return found
    return None


def tiled_windows(node):
    """Return tiled leaf window IDs, deliberately excluding floating_nodes."""
    found = []
    for child in node.get("nodes", []):
        if child.get("type") == "con" and not child.get("nodes"):
            if child.get("pid") is not None or child.get("app_id") or child.get("window_properties"):
                found.append(child["id"])
        found.extend(tiled_windows(child))
    return found


def command(text):
    output = sway(text)
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        return
    if isinstance(result, list):
        failures = [item for item in result if isinstance(item, dict) and not item.get("success", True)]
        if failures:
            message = "; ".join(item.get("error", "Sway command failed") for item in failures)
            raise RuntimeError(f"{text}: {message}")


def command_sequence(commands):
    """Run a sequence as one Sway IPC request to reduce intermediate redraws.

    Semicolons are intentional: unlike commas, they reset criteria between
    commands, so one [con_id=...] selector cannot leak into the next command.
    Sway commits the resulting tree transaction after processing the request.
    """
    commands = list(commands)
    if not commands:
        return
    sequence = "; ".join(commands)
    output = sway(sequence)
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        return
    if isinstance(result, list):
        failures = [
            (index, item) for index, item in enumerate(result)
            if isinstance(item, dict) and not item.get("success", True)
        ]
        if failures:
            details = []
            for index, item in failures:
                error = item.get("error", "Sway command failed")
                context = commands[index] if index < len(commands) else sequence
                details.append(f"{context}: {error}")
            raise RuntimeError("; ".join(details))


def tiled_walk(node, depth=0):
    """Walk only tiled containers, excluding floating_nodes."""
    yield node, depth
    for child in node.get("nodes", []):
        yield from tiled_walk(child, depth + 1)


def find_node(node, con_id):
    for current in walk(node):
        if current.get("id") == con_id:
            return current
    return None


def right_split_container(ws):
    """Find the outermost multi-window tiled container occupying the right side.

    This intentionally ignores focus: Mod+T always means "toggle the right group",
    even if focus is on the leftmost window.  It also ignores floating windows.
    """
    ws_rect = ws.get("rect", {})
    ws_x = ws_rect.get("x", 0)
    ws_width = ws_rect.get("width", 0)
    if ws_width <= 0:
        return None
    midpoint = ws_x + ws_width * 0.5
    # Allow a little tolerance for borders/rounding around a 50:50 split.
    right_edge_threshold = midpoint - ws_width * 0.03

    candidates = []
    for node, depth in tiled_walk(ws):
        if node.get("type") != "con":
            continue
        # Layout commands act on the parent of the targeted child. Only pick
        # a group that directly contains at least two branches; targeting a
        # one-child wrapper can make recent Sway versions flatten the tree
        # upward and create a tab bar on the workspace/parent by mistake.
        if len(node.get("nodes", [])) < 2:
            continue
        window_ids = tiled_windows(node)
        if len(window_ids) < 2:
            continue
        rect = node.get("rect", {})
        x = rect.get("x", ws_x)
        width = rect.get("width", 0)
        if x >= right_edge_threshold:
            # The shallowest matching node is the full right-hand group.  If
            # equally deep, prefer the wider container over a nested subgroup.
            candidates.append((depth, -width, x, node))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def layout_parent_command(container, layout):
    """Build a command that changes a container by targeting one of its children.

    In Sway, `layout ...` changes the *parent of the matched/focused node*.
    Targeting the group container itself would therefore change the workspace
    layout, accidentally tabbing the left window together with the right group.
    """
    children = container.get("nodes", [])
    if not children:
        raise RuntimeError(f'Container {container.get("id")} has no tiled children')
    child_id = children[0].get("id")
    if child_id is None:
        raise RuntimeError(f'Could not identify a child of container {container.get("id")}')
    return f'[con_id={child_id}] layout {layout}'


def layout_parent_via_child(container, layout):
    command(layout_parent_command(container, layout))


def border_command(con_id, border, width=2):
    """Build a border command that restores a window's prior decoration."""
    if border not in {"none", "normal", "pixel", "csd"}:
        border = "normal"
    try:
        width = max(0, int(width))
    except (TypeError, ValueError):
        width = 2
    if border in {"normal", "pixel"}:
        return f"[con_id={con_id}] border {border} {width}"
    return f"[con_id={con_id}] border {border}"


def single_child_split_wrapper_depth(branch, leaf_id):
    """Count collapsible split wrappers between a branch and one leaf.

    Sway can leave layout wrappers like V[Window] in the tree. When such a
    wrapper is used as a tab child, Sway displays a [v] prefix in the tab title.
    `split none` removes a split parent when its current container is that
    parent's only child, so issuing it once per wrapper promotes the real window
    to the right-hand group without touching the left side.
    """
    depth = 0
    node = branch
    while node and node.get("id") != leaf_id:
        children = node.get("nodes", [])
        if node.get("layout") not in {"splith", "splitv"} or len(children) != 1:
            return 0
        depth += 1
        node = children[0]
    return depth if node and node.get("id") == leaf_id else 0


def collapsible_split_wrappers(target):
    """Return (leaf ID, depth) for right-group branches that are wrapper-only."""
    result = []
    for branch in target.get("nodes", []):
        leaf_ids = tiled_windows(branch)
        if len(leaf_ids) != 1:
            continue
        depth = single_child_split_wrapper_depth(branch, leaf_ids[0])
        if depth:
            result.append((leaf_ids[0], depth))
    return result


def toggle_right_group(ws, name, state_dir, key):
    """Toggle only the right-side split, restoring its previous layout next time."""
    state_path = os.path.join(state_dir, key + ".right-tabs.json")

    # If we toggled this group earlier, restore its saved layout by exact ID.
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            target = find_node(ws, state.get("container_id"))
            # Only change a container that has at least two direct child
            # branches. With a one-child wrapper, Sway may flatten upward
            # while applying `layout`, accidentally changing the workspace or
            # an unrelated parent container.
            if (
                target
                and target.get("type") == "con"
                and len(target.get("nodes", [])) >= 2
            ):
                original_layout = state.get("original_layout", "splitv")
                if original_layout not in {"splith", "splitv", "tabbed", "stacked"}:
                    original_layout = "splitv"
                commands = [layout_parent_command(target, original_layout)]
                # Restore any nested leaf borders that were temporarily changed
                # to pixel style to avoid a duplicate inner titlebar.
                for saved_border in state.get("extra_borders", []):
                    con_id = saved_border.get("con_id")
                    if con_id is not None and find_node(ws, con_id):
                        commands.append(border_command(
                            con_id,
                            saved_border.get("border", "normal"),
                            saved_border.get("border_width", 2),
                        ))
                commands.append("hide_edge_borders --i3 none")
                command_sequence(commands)
                os.unlink(state_path)
                return 0
        except (OSError, ValueError, TypeError):
            pass
        # The tree may have changed (for example, after global monocle mode).
        try:
            os.unlink(state_path)
        except FileNotFoundError:
            pass

    target = right_split_container(ws)
    if target is None:
        print(
            "No right-hand tiled group with at least two windows was found; nothing changed.",
            file=sys.stderr,
        )
        return 0

    original_layout = target.get("layout", "splitv")
    if original_layout not in {"splith", "splitv", "tabbed", "stacked"}:
        original_layout = "splitv"

    # Normally this changes splitv -> tabbed. If the right group was already
    # tabbed before this binding was used, switch it to splitv temporarily;
    # the next press restores its pre-toggle layout.
    new_layout = "splitv" if original_layout == "tabbed" else "tabbed"
    extra_borders = []
    if new_layout == "tabbed":
        # Flatten single-child split wrappers before tabbing. Otherwise each
        # tab may represent V[Window] rather than the window itself, yielding
        # unwanted [v] prefixes and ghost parent-titlebar behavior.
        commands = []
        for leaf_id, depth in collapsible_split_wrappers(target):
            commands.extend(f"[con_id={leaf_id}] split none" for _ in range(depth))

        # After wrappers collapse, target's direct children are the real window
        # leaves. Target one of those leaves so `layout tabbed` changes target,
        # not the workspace and not an obsolete wrapper ID.
        window_ids = tiled_windows(target)
        if not window_ids:
            print("The right-hand group has no tiled windows; nothing changed.", file=sys.stderr)
            return 0
        commands.append(f"[con_id={window_ids[0]}] layout tabbed")
    else:
        commands = [layout_parent_command(target, new_layout)]
    # Keep the existing handling for one-child tab containers.
    commands.append("hide_edge_borders --i3 none")
    command_sequence(commands)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "workspace": name,
                "container_id": target["id"],
                "original_layout": original_layout,
                "extra_borders": extra_borders,
            },
            f,
        )
    return 0

def main():
    parser = argparse.ArgumentParser(description="Toggle Sway workspace monocle/tabbed layouts")
    parser.add_argument("--right-toggle", action="store_true", help="toggle tabbing of the right-hand tiled group only")
    args = parser.parse_args()

    tree = json.loads(sway("-t", "get_tree"))
    ws = focused_workspace(tree)
    if not ws:
        print("Could not find the focused workspace.", file=sys.stderr)
        return 1

    name = ws.get("name", "")
    ids = tiled_windows(ws)
    if not ids:
        return 0

    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/sway-monocle-{os.getuid()}")
    state_dir = os.path.join(runtime, "sway-monocle-toggle")
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    key = hashlib.sha256(name.encode()).hexdigest()
    state_path = os.path.join(state_dir, key + ".json")
    active = os.path.exists(state_path)

    if args.right_toggle:
        return toggle_right_group(ws, name, state_dir, key)

    # If the previous toggle only hid a single window's title bar, restore the
    # exact original border style now. Do this even if another window was opened
    # in the meantime: the single-window toggle should not unexpectedly re-tile
    # the workspace.
    single_state = None
    if active:
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                saved_state = json.load(f)
            if saved_state.get("mode") == "single-window-border":
                single_state = saved_state
        except (OSError, ValueError, TypeError):
            pass

    if single_state is not None:
        con_id = single_state.get("con_id")
        border = single_state.get("border", "normal")
        width = single_state.get("border_width", 2)
        if border not in {"none", "normal", "pixel", "csd"}:
            border = "normal"
        try:
            width = max(0, int(width))
        except (TypeError, ValueError):
            width = 2
        if border in {"normal", "pixel"}:
            command(f"[con_id={con_id}] border {border} {width}")
        else:
            command(f"[con_id={con_id}] border {border}")
        try:
            os.unlink(state_path)
        except FileNotFoundError:
            pass
        return 0

    # With just one tiled window, monocle mode means hiding that window's title
    # bar. Save the existing border style so the next press restores it.
    if len(ids) == 1 and not active:
        node = find_node(ws, ids[0])
        if node is None:
            return 0
        border = node.get("border", "normal")
        if border not in {"none", "normal", "pixel", "csd"}:
            border = "normal"
        width = node.get("current_border_width", 2)
        try:
            width = max(0, int(width))
        except (TypeError, ValueError):
            width = 2
        # `pixel` keeps the existing border thickness but removes the title bar.
        command(f"[con_id={ids[0]}] border pixel {width}")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({
                "workspace": name,
                "active": True,
                "mode": "single-window-border",
                "con_id": ids[0],
                "border": border,
                "border_width": width,
            }, f)
        return 0

    # An empty workspace needs no action. If there's one window and the saved
    # state came from a multi-window monocle toggle, continue through the normal
    # restoration path below.
    if not ids:
        return 0

    focused_id = next((n["id"] for n in walk(ws) if n.get("focused") and n.get("type") == "con"), None)
    stage = f"__sway_monocle_stage_{os.getpid()}_{time.time_ns()}"

    # Build the complete re-layout as a single IPC request. Sway processes all
    # commands before committing the resulting tree transaction, avoiding most
    # intermediate redraws where windows briefly disappear during staging.
    cmds = [
        f'[con_id={con_id}] move container to workspace "{stage}"'
        for con_id in ids
    ]
    cmds.append(f'workspace "{name}"')

    if not active:
        # Enter monocle: flatten every tiled window into one workspace-wide tab group.
        cmds.append("layout tabbed")
        cmds.extend(
            f'[con_id={con_id}] move container to workspace "{name}"'
            for con_id in ids
        )
    else:
        # Restore a useful default arrangement:
        #   1–2 windows: side-by-side
        #   3+ windows: first window on the left, remaining windows stacked right.
        # Commands stay in one IPC request to minimize visible intermediate states.
        cmds.append("layout splith")
        if len(ids) >= 3:
            cmds.append(f'[con_id={ids[0]}] move container to workspace "{name}"')
            cmds.append(f'[con_id={ids[1]}] move container to workspace "{name}"')
            cmds.append(f'[con_id={ids[1]}] focus')
            cmds.append(f'[con_id={ids[1]}] splitv')
            insertion_target = ids[1]
            for con_id in ids[2:]:
                cmds.append(f'[con_id={insertion_target}] focus')
                cmds.append(f'[con_id={con_id}] move container to workspace "{name}"')
                insertion_target = con_id
            # Keep the master window and right-hand stack balanced.
            # autotiling --splitratio controls orientation rather than directly
            # assigning a fixed 61:39 width, so use an even left/right split.
            cmds.append(f'[con_id={ids[0]}] resize set width 50 ppt')
        else:
            cmds.extend(
                f'[con_id={con_id}] move container to workspace "{name}"'
                for con_id in ids
            )

    if focused_id is not None:
        cmds.append(f"[con_id={focused_id}] focus")

    command_sequence(cmds)

    if active:
        try:
            os.unlink(state_path)
        except FileNotFoundError:
            pass
    else:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"workspace": name, "active": True, "mode": "workspace-tabs"}, f)
    return 0


def walk(node):
    yield node
    for child in node.get("nodes", []):
        yield from walk(child)
    for child in node.get("floating_nodes", []):
        yield from walk(child)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"sway-monocle-toggle: {exc}", file=sys.stderr)
        raise SystemExit(1)
