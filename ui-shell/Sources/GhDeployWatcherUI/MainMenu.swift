import AppKit

/// Minimal main menu. Without it a programmatic AppKit app has no Cmd-C/V/X/A,
/// undo, Cmd-W or Cmd-Q. Edit items target the first responder (nil target).
func buildMainMenu() -> NSMenu {
    let main = NSMenu()

    func add(_ title: String, key: String, action: Selector, to menu: NSMenu,
             modifiers: NSEvent.ModifierFlags = [.command]) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = modifiers
        menu.addItem(item)
    }
    func submenu(_ title: String) -> NSMenu {
        let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        holder.submenu = menu
        main.addItem(holder)
        return menu
    }

    let app = submenu("gh-deploy-watcher")
    add("Quit", key: "q", action: #selector(NSApplication.terminate(_:)), to: app)

    let file = submenu("File")
    add("Close Window", key: "w", action: #selector(NSWindow.performClose(_:)), to: file)

    let edit = submenu("Edit")
    add("Undo", key: "z", action: Selector(("undo:")), to: edit)
    add("Redo", key: "z", action: Selector(("redo:")), to: edit, modifiers: [.command, .shift])
    edit.addItem(.separator())
    add("Cut", key: "x", action: #selector(NSText.cut(_:)), to: edit)
    add("Copy", key: "c", action: #selector(NSText.copy(_:)), to: edit)
    add("Paste", key: "v", action: #selector(NSText.paste(_:)), to: edit)
    add("Select All", key: "a", action: #selector(NSText.selectAll(_:)), to: edit)
    return main
}

/// Checks the menu structure without a display or a running app.
func runMenuSelfTest() -> [String] {
    var failures: [String] = []
    let menu = buildMainMenu()
    var found: [String: (String, Selector?, NSEvent.ModifierFlags)] = [:]
    for top in menu.items {
        for item in top.submenu?.items ?? [] where !item.isSeparatorItem {
            found[item.title] = (item.keyEquivalent, item.action, item.keyEquivalentModifierMask)
        }
    }
    let expected: [(String, String, Selector, NSEvent.ModifierFlags)] = [
        ("Quit", "q", #selector(NSApplication.terminate(_:)), [.command]),
        ("Close Window", "w", #selector(NSWindow.performClose(_:)), [.command]),
        ("Undo", "z", Selector(("undo:")), [.command]),
        ("Redo", "z", Selector(("redo:")), [.command, .shift]),
        ("Cut", "x", #selector(NSText.cut(_:)), [.command]),
        ("Copy", "c", #selector(NSText.copy(_:)), [.command]),
        ("Paste", "v", #selector(NSText.paste(_:)), [.command]),
        ("Select All", "a", #selector(NSText.selectAll(_:)), [.command]),
    ]
    for (title, key, action, mods) in expected {
        guard let got = found[title] else { failures.append("menu item missing: " + title); continue }
        if got.0 != key { failures.append("menu key for " + title) }
        if got.1 != action { failures.append("menu action for " + title) }
        if got.2 != mods { failures.append("menu modifiers for " + title) }
    }
    return failures
}
