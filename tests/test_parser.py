from nobox import parser


def test_trim_strips_quoted_reply():
    body = (
        "Hi there, my reply.\n"
        "\n"
        "On Wed, May 21, 2026 at 1:23 PM, Foo <foo@bar> wrote:\n"
        "> previous message\n"
        "> with multiple lines\n"
    )
    assert "my reply" in parser.trim_reply(body)
    assert "previous message" not in parser.trim_reply(body)


def test_trim_strips_signature():
    body = (
        "Real content here.\n"
        "More content.\n"
        "-- \n"
        "Bob\n"
        "Phone: 555\n"
    )
    out = parser.trim_reply(body)
    assert "Real content" in out
    assert "Bob" not in out


def test_trim_strips_trailing_quotes():
    body = "My reply.\n> quoted\n> more quoted\n"
    out = parser.trim_reply(body)
    assert "My reply" in out
    assert "quoted" not in out


def test_trim_preserves_markdown_image():
    body = (
        "Look at this:\n"
        "![image](https://user-images.githubusercontent.com/123/abc.png)\n"
        "\n"
        "On Mon, wrote:\n"
        "> quoted\n"
    )
    out = parser.trim_reply(body)
    assert "user-images.githubusercontent.com" in out


def test_trim_handles_empty():
    assert parser.trim_reply("") == ""
