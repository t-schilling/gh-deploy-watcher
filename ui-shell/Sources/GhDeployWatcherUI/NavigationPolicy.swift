import Foundation

/// Maximum accepted length of the start URL line, in bytes.
let maxStartURLBytes = 2048

/// True only for `http://127.0.0.1:<port>/...` without credentials.
func isAppURL(_ url: URL, port: Int) -> Bool {
    guard url.scheme == "http",
          url.host == "127.0.0.1",
          url.port == port,
          url.user == nil, url.password == nil else { return false }
    return true
}

/// Parses the single stdin line. Returns nil unless it is exactly
/// `http://127.0.0.1:<port>/...` with a port in 1...65535. The fragment is kept.
func parseStartURL(_ line: String) -> URL? {
    let prefix = "http://127.0.0.1:"
    guard !line.isEmpty, line.utf8.count <= maxStartURLBytes, line.hasPrefix(prefix) else { return nil }
    // Printable ASCII only: no control characters, whitespace, backslash or non-ASCII.
    for b in line.utf8 where b <= 0x20 || b >= 0x7F || b == 0x5C { return nil }
    let rest = line.dropFirst(prefix.count)
    guard let slash = rest.firstIndex(of: "/") else { return nil }
    let digits = rest[rest.startIndex..<slash]
    guard !digits.isEmpty, digits.count <= 5, digits.allSatisfy({ $0 >= "0" && $0 <= "9" }),
          let port = Int(digits), (1...65535).contains(port) else { return nil }
    guard let url = URL(string: line), isAppURL(url, port: port) else { return nil }
    return url
}

/// Pure policy checks, run by `--selftest` (XCTest is unavailable with only the
/// Command Line Tools). Returns the list of failure descriptions.
func runSelfTest() -> [String] {
    var failures: [String] = []
    func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }

    let good = "http://127.0.0.1:8123/#tok-abc"
    let parsed = parseStartURL(good)
    check(parsed != nil, "valid URL accepted")
    check(parsed?.fragment == "tok-abc", "fragment kept")
    check(parseStartURL("http://127.0.0.1:1/") != nil, "port 1 accepted")
    check(parseStartURL("http://127.0.0.1:65535/x?y=1#t") != nil, "port 65535 accepted")

    let bad: [(String, String)] = [
        ("", "empty"),
        ("http://localhost:8123/", "localhost"),
        ("http://example.com:8123/", "other host"),
        ("https://127.0.0.1:8123/", "https scheme"),
        ("file:///etc/passwd", "file scheme"),
        ("javascript:alert(1)", "javascript scheme"),
        ("http://127.0.0.1/", "no port"),
        ("http://127.0.0.1:/", "empty port"),
        ("http://127.0.0.1:0/", "port 0"),
        ("http://127.0.0.1:65536/", "port too large"),
        ("http://127.0.0.1:80a/", "non-numeric port"),
        ("http://127.0.0.1:+80/", "signed port"),
        ("http://127.0.0.1:8123", "no path"),
        ("http://[::1]:8123/", "ipv6"),
        ("http://[::ffff:127.0.0.1]:8123/", "ipv4-mapped ipv6"),
        ("http://user@127.0.0.1:8123/", "userinfo"),
        ("http://127.0.0.1:8123@evil.com/", "userinfo trick"),
        ("http://127.0.0.1:8123/\nx", "newline"),
        ("http://127.0.0.1:8123/\tx", "tab"),
        ("http://127.0.0.1:8123/ x", "inner space"),
        ("http://127.0.0.1:8123/\u{7F}", "DEL"),
        ("http://127.0.0.1:8123/\u{00E9}", "non-ascii"),
        ("http://127.0.0.1:8123\\@evil.com/", "backslash"),
        ("http://127.0.0.1:8123/" + String(repeating: "a", count: 2100), "over-long"),
        (" http://127.0.0.1:8123/", "leading space"),
    ]
    for (line, name) in bad { check(parseStartURL(line) == nil, "rejected: " + name) }

    if let u = parsed {
        check(isAppURL(u, port: 8123), "isAppURL same origin")
        check(!isAppURL(u, port: 8124), "isAppURL wrong port")
    }
    let others = ["http://localhost:8123/", "https://127.0.0.1:8123/", "http://127.0.0.1:9/",
                  "http://user@127.0.0.1:8123/", "file:///tmp/x", "https://example.com/"]
    if let u = URL(string: "HTTP://127.0.0.1:8123/") { check(!isAppURL(u, port: 8123), "isAppURL rejects upper-case scheme") }
    check(parseStartURL("HTTP://127.0.0.1:8123/") == nil, "upper-case scheme rejected")
    check(parseStartURL("http://127.0.0.1:80@evil.com/") == nil, "userinfo after port")
    check(parseStartURL("http://evil.com#@127.0.0.1:80/") == nil, "fragment trick")
    for s in others {
        if let u = URL(string: s) { check(!isAppURL(u, port: 8123), "isAppURL rejects " + s) }
    }
    return failures
}
