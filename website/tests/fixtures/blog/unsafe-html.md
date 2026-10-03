---
title: Unsafe HTML
date: 2026-09-01
summary: A test-only fixture that tries to slip raw HTML into a post.
---

<script>alert("block")</script>

Inline <img src="x" onerror="alert('inline')"> image and <b onclick="alert(1)">bold</b> tag.

<iframe src="https://example.com"></iframe>

[a javascript link](<javascript:alert('link')>) and ![a data image](data:text/html;base64,PHNjcmlwdD4=)
