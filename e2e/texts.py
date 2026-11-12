"""Test chapter content shared by the stand-in pipeline and the browser test."""
THAI = "สวัสดีครับ นี่คือบทที่หนึ่ง\u200b ผู้ชาย: “ไปไหนมา?” — ฉันถามเบา ๆ"
COMPLEX = (
    "   \tleading spaces and a tab\r\n"
    f"{THAI}\n"
    "English line with punctuation: !?;,.…\u00a0(non-breaking space)\n"
    "Emoji: 😀🔥👩\u200d👩\u200d👧\u200d👦 ❤\ufe0f 🇹🇭\n"
    "NFD e + combining acute: e\u0301 | ANGSTROM SIGN: \u212b | ligature: \ufb01 | ZWJ:\u200d|\n"
    "\n\n\n"
    "Paragraph two after three blank lines.\r\n\r\n"
    "[image: https://cdn.example.com/img/ch1/pic_001.jpg]\n"
    "[รูปภาพ] https://www.readawrite.com/c/669e6e939a5f5c79b7eb5ee2ce220116?x=1&y=%E0%B8%81\n"
    "Backslashes \\ and quotes \" ' and <html>&amp; {json: \"x\"} 100% $HOME `cmd`\n"
    "trailing spaces on last line   \n\n  "
)
SECOND = "บทที่สอง\nSecond chapter — plain.\n\n\nEnd.   "
THIRD = "Chapter three\nฉากสุดท้าย 🎉\n"
TEXTS = {1: COMPLEX, 2: SECOND, 3: THIRD}
