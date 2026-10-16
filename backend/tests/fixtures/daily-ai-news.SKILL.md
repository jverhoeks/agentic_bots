---
name: daily-ai-news
description: Daily digest of the most important AI news on Hacker News from the last 24 hours.
allowed-tools: hackernews_hn_top_stories hackernews_hn_item web_fetch_url web_web_search
metadata:
  title: Daily AI News
  model: claude-sonnet
  trigger: manual
  max-steps: '25'
---

# Goal
Give a busy engineer a 3-minute read of what happened in AI on Hacker News in the last 24 hours.

# Steps
1. Call `hackernews_hn_top_stories` with `since_hours=24` and no query filter, so nothing is missed.
2. Select the AI-related stories by judgement: models and releases, LLMs, agents, AI research,
   AI tooling and infrastructure, chips, AI policy/regulation, notable AI products or failures.
3. Rank by significance first, then points and comment count. Keep the top 8-12.
4. For the top 5, read the article with `web_fetch_url` and/or the discussion with `hackernews_hn_item`
   to get the substance and the community's take.

# Output format
```
# AI news, <date>
**TL;DR**: 2-3 sentences on the big picture.

## Top stories
1. **[Title](article url)**: 2-3 sentences of what it is and why it matters.
   *HN: <points> points, <comments> comments ([discussion](hn url))*. One line on the community's take.

## Also notable
- **[Title](url)**: one line. ([HN](hn url))
```

# Quality rules
- No duplicates (the same news from several sources = one item, mention the other links).
- Facts come from the article or discussion, not guesses. Always link.
- If it was a quiet day, say so rather than padding the list.
