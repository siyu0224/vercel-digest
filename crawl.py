#!/usr/bin/env python3
"""
Vercel Digest Crawler
Fetches discussions from Reddit, Hacker News, GitHub, and X and summarizes with Claude.
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

HEADERS = {"User-Agent": "vercel-digest-bot/1.0"}
VERCEL_KEYWORDS = ["vercel", "vercel ai", "vercel platform", "next.js vercel", "nextjs vercel"]

# ── GitHub ────────────────────────────────────────────────────────────────────

def gh_headers():
    token = os.getenv("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github+json", "User-Agent": "vercel-digest-bot/1.0"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def github_fetch(time_filter="month", limit=30):
    days = {"day": 1, "week": 7, "month": 30}.get(time_filter, 30)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = [
        f"repo:vercel/next.js is:issue created:>{since} reactions:>3",
        f"vercel deployment is:issue created:>{since} reactions:>5",
    ]

    all_issues, seen = [], set()
    for q in queries:
        url = "https://api.github.com/search/issues"
        params = {"q": q, "sort": "reactions", "order": "desc", "per_page": limit}
        try:
            r = requests.get(url, headers=gh_headers(), params=params, timeout=10)
            r.raise_for_status()
            items = r.json().get("items", [])
            for issue in items:
                if issue["id"] not in seen:
                    seen.add(issue["id"])
                    all_issues.append(issue)
            print(f"  ✅  Query '{q[:50]}…': {len(items)} issues")
        except Exception as e:
            print(f"  ⚠️  {e}")
        time.sleep(1)

    def is_vercel(i):
        text = (i.get("title", "") + " " + (i.get("body") or "")).lower()
        return any(kw in text for kw in VERCEL_KEYWORDS)

    all_issues = [i for i in all_issues if is_vercel(i)]
    all_issues.sort(key=lambda i: i.get("reactions", {}).get("total_count", 0), reverse=True)
    return all_issues[:limit]


def github_extract(issue):
    reactions = issue.get("reactions", {})
    labels = [l["name"] for l in issue.get("labels", [])]
    repo_url = issue.get("repository_url", "")
    repo_name = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
    return {
        "source":       "github",
        "title":        issue.get("title", ""),
        "subreddit":    "",
        "repo":         repo_name,
        "score":        reactions.get("+1", 0) + reactions.get("total_count", 0),
        "reactions":    reactions.get("total_count", 0),
        "num_comments": issue.get("comments", 0),
        "url":          issue.get("html_url", ""),
        "date":         (issue.get("created_at") or "")[:10],
        "state":        issue.get("state", ""),
        "labels":       labels,
        "body":         (issue.get("body") or "")[:600].strip(),
        "comments":     [],
    }

# ── Reddit ────────────────────────────────────────────────────────────────────

SUBREDDITS = [
    "vercel", "nextjs", "webdev", "javascript",
    "reactjs", "node", "devops", "programming",
]

def reddit_fetch(time_filter="month", posts_per_sub=10):
    all_posts, seen = [], set()
    for sub in SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/search.json"
        params = {"q": "vercel", "sort": "top", "t": time_filter,
                  "limit": posts_per_sub, "restrict_sr": "true"}
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            r.raise_for_status()
            posts = r.json().get("data", {}).get("children", [])
            for item in posts:
                p = item["data"]
                if p["id"] not in seen:
                    seen.add(p["id"])
                    all_posts.append(p)
            print(f"  ✅  r/{sub}: {len(posts)} posts")
        except Exception as e:
            print(f"  ⚠️  r/{sub}: {e}")
        time.sleep(1)

    def is_vercel(p):
        text = (p.get("title", "") + " " + p.get("selftext", "")).lower()
        return any(kw in text for kw in VERCEL_KEYWORDS)

    all_posts = [p for p in all_posts if is_vercel(p)]
    all_posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return all_posts


def reddit_comments(permalink, limit=6):
    try:
        r = requests.get(f"https://www.reddit.com{permalink}.json",
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        items = r.json()[1]["data"]["children"]
        out = []
        for item in items:
            if item["kind"] != "t1":
                continue
            c = item["data"]
            body = c.get("body", "")
            if body not in ("[deleted]", "[removed]", ""):
                out.append({"score": c.get("score", 0), "body": body[:500].strip()})
            if len(out) >= limit:
                break
        return sorted(out, key=lambda x: x["score"], reverse=True)
    except Exception:
        return []


def reddit_extract(p, fetch_cmts=True):
    permalink = p.get("permalink", "")
    comments = reddit_comments(permalink) if fetch_cmts else []
    if fetch_cmts:
        time.sleep(0.8)
    return {
        "source": "reddit",
        "title": p.get("title", ""),
        "subreddit": p.get("subreddit", ""),
        "score": p.get("score", 0),
        "num_comments": p.get("num_comments", 0),
        "url": f"https://reddit.com{permalink}",
        "date": datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc).strftime("%Y-%m-%d"),
        "body": (p.get("selftext", "") or "")[:600].strip(),
        "comments": comments,
    }


# ── Hacker News ───────────────────────────────────────────────────────────────

def hn_fetch(time_filter="month", limit=30):
    days = {"day": 1, "week": 7, "month": 30}.get(time_filter, 30)
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    url = "https://hn.algolia.com/api/v1/search"
    params = {
        "query": "vercel deployment",
        "tags": "story",
        "hitsPerPage": limit,
        "numericFilters": f"created_at_i>{since},points>5",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        print(f"  ✅  Hacker News: {len(hits)} stories")
        return hits
    except Exception as e:
        print(f"  ⚠️  Hacker News: {e}")
        return []


def hn_comments(story_id, limit=6):
    try:
        r = requests.get(f"https://hn.algolia.com/api/v1/items/{story_id}", timeout=10)
        r.raise_for_status()
        children = r.json().get("children", [])
        out = []
        for c in children:
            text = c.get("text", "") or ""
            if text and c.get("type") == "comment":
                out.append({"score": c.get("points") or 0, "body": text[:500].strip()})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def hn_extract(hit, fetch_cmts=True):
    story_id = hit.get("objectID", "")
    comments = hn_comments(story_id) if fetch_cmts else []
    if fetch_cmts:
        time.sleep(0.5)

    title = hit.get("title", "")
    text = (title + " " + (hit.get("story_text") or "")).lower()
    if not any(kw in text for kw in VERCEL_KEYWORDS):
        return None

    created = hit.get("created_at_i", 0)
    return {
        "source": "hn",
        "title": title,
        "subreddit": "",
        "score": hit.get("points", 0),
        "num_comments": hit.get("num_comments", 0),
        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={story_id}",
        "hn_url": f"https://news.ycombinator.com/item?id={story_id}",
        "date": datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d") if created else "",
        "body": (hit.get("story_text") or "")[:600].strip(),
        "comments": comments,
    }


# ── X / Twitter ───────────────────────────────────────────────────────────────

def twitter_fetch(limit=100):
    token = os.getenv("TWITTER_BEARER_TOKEN")
    if not token:
        print("  ⚠️  No TWITTER_BEARER_TOKEN — skipping X")
        return [], {}

    headers = {"Authorization": f"Bearer {token}"}
    query = '(vercel OR "vercel deploy" OR "vercel ai") -is:retweet -is:reply lang:en'
    params = {
        "query": query,
        "max_results": min(limit, 100),
        "tweet.fields": "public_metrics,created_at,author_id,text",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        tweets = data.get("data", [])
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        print(f"  ✅  X/Twitter: {len(tweets)} tweets")
        return tweets, users
    except Exception as e:
        print(f"  ⚠️  X/Twitter: {e}")
        return [], {}


def twitter_extract(tweet, users):
    metrics = tweet.get("public_metrics", {})
    author = users.get(tweet.get("author_id", ""), {})
    username = author.get("username", "unknown")
    tweet_id = tweet.get("id", "")
    likes = metrics.get("like_count", 0)
    retweets = metrics.get("retweet_count", 0)
    created = tweet.get("created_at", "")[:10]
    return {
        "source": "twitter",
        "title": tweet.get("text", "")[:120],
        "subreddit": "",
        "score": likes + retweets * 2,
        "likes": likes,
        "retweets": retweets,
        "num_comments": metrics.get("reply_count", 0),
        "url": f"https://x.com/{username}/status/{tweet_id}",
        "date": created,
        "body": tweet.get("text", ""),
        "username": username,
    }


# ── Claude Summarizer ─────────────────────────────────────────────────────────

def build_prompt(posts, time_filter, source_label):
    lines = [f"Below are the top {source_label} discussions about Vercel (the deployment platform) from the past {time_filter}.\n"]
    for i, p in enumerate(posts, 1):
        lines.append(f"── {i} ──────────────────")
        lines.append(f"Title:    {p['title']}")
        if p.get("subreddit"):
            lines.append(f"Sub:      r/{p['subreddit']}  |  Score: {p['score']}  |  {p['date']}")
        else:
            lines.append(f"Points:   {p['score']}  |  {p['date']}")
        lines.append(f"URL:      {p['url']}")
        if p.get("body"):
            lines.append(f"Body:     {p['body']}")
        for c in p.get("comments", []):
            lines.append(f"  [{c['score']:+d}] {c['body']}")
        lines.append("")
    lines.append(
        "Produce a concise Markdown summary:\n\n"
        "## Key Themes\n## Overall Sentiment\n## Top Complaints & Issues\n"
        "## Top Praise\n## Interesting Debates\n## Must-Read Posts\n"
        "3–5 standout posts with URLs. Keep it tight."
    )
    return "\n".join(lines)


def build_github_prompt(issues, time_filter):
    lines = [
        f"Below are GitHub issues from the past {time_filter} related to Vercel.\n"
        f"Sources: vercel/next.js (the main Next.js repo) and broader GitHub search for Vercel deployment issues.\n"
    ]
    for i, p in enumerate(issues, 1):
        lines.append(f"── Issue {i} ──────────────────")
        lines.append(f"Title:     {p['title']}")
        lines.append(f"Repo:      {p['repo']}  |  👍 {p['reactions']}  |  💬 {p['num_comments']}  |  {p['date']}")
        lines.append(f"State:     {p['state']}  |  Labels: {', '.join(p['labels']) or 'none'}")
        lines.append(f"URL:       {p['url']}")
        if p.get("body"):
            lines.append(f"Body:      {p['body']}")
        lines.append("")
    lines.append(
        "Produce a concise Markdown summary:\n\n"
        "## Most Reported Bugs\n"
        "Top recurring bugs with reaction counts. Flag anything with 👍>20 as high priority.\n\n"
        "## Most Requested Features\n"
        "What users are asking for most.\n\n"
        "## ⚠️ Concerns Worth Flagging\n"
        "Anything alarming — long-unresolved bugs, silent regressions, or trust-eroding patterns.\n\n"
        "## Team Responsiveness\n"
        "Are teams responding? Any official fixes?\n\n"
        "## Must-Read Issues\n"
        "3–5 standout issues with GitHub URLs.\n\nKeep it tight."
    )
    return "\n".join(lines)


def build_twitter_prompt(posts, time_filter):
    lines = [f"Below are recent X/Twitter posts about Vercel (the deployment platform).\n"]
    for i, p in enumerate(posts, 1):
        lines.append(f"── {i} ──────────────────")
        lines.append(f"@{p['username']}  |  ❤️ {p['likes']}  🔁 {p['retweets']}  |  {p['date']}")
        lines.append(f"URL: {p['url']}")
        lines.append(p["body"])
        lines.append("")
    lines.append(
        "Produce a concise Markdown summary:\n\n"
        "## Key Themes\n## Overall Sentiment\n## Top Complaints & Issues\n"
        "## Top Praise\n## Viral Moments\n## Must-Read Posts\n"
        "3–5 standout tweets with URLs. Keep it tight."
    )
    return "\n".join(lines)


def build_overview_prompt(reddit_summary, hn_summary, github_summary, twitter_summary, time_filter):
    twitter_section = f"\n## X/Twitter Summary\n{twitter_summary}" if twitter_summary else ""
    return f"""Below are summaries of Vercel discussions from Reddit, Hacker News, GitHub Issues, and X/Twitter over the past {time_filter}.

## Reddit Summary
{reddit_summary}

## Hacker News Summary
{hn_summary}

## GitHub Issues Summary
{github_summary}{twitter_section}

Now write a combined overview synthesizing all sources:

## What Everyone's Talking About
Dominant themes across all communities.

## Consensus Views
Where Reddit, HN, GitHub, and X all agree.

## Where They Diverge
Interesting differences in perspective between communities.

## Overall Sentiment
Combined read across all platforms.

## Top Signals This Month
The most important takeaways a product team should know.

## Must-Read Posts & Issues
Best 3–5 items from any source with URLs. Keep it tight."""


def claude_summarize(prompt, label):
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌  Missing ANTHROPIC_API_KEY in .env")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)
    print(f"\n🤖  Summarizing {label} with Claude…")
    resp = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ── Output ────────────────────────────────────────────────────────────────────

def to_publish_post(p):
    return {
        "source":       p["source"],
        "title":        p["title"],
        "subreddit":    p.get("subreddit", ""),
        "score":        p["score"],
        "num_comments": p["num_comments"],
        "url":          p["url"],
        "hn_url":       p.get("hn_url", ""),
        "date":         p["date"],
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--time",         choices=["day", "week", "month"], default="month")
    parser.add_argument("--limit",        type=int, default=10)
    parser.add_argument("--no-comments",  action="store_true")
    parser.add_argument("--no-summarize", action="store_true")
    parser.add_argument("--publish",      action="store_true")
    args = parser.parse_args()

    fetch_cmts = not args.no_comments

    # ── Reddit ──
    print(f"\n🔍  Reddit (past {args.time})…")
    raw_reddit = reddit_fetch(time_filter=args.time, posts_per_sub=args.limit)
    print(f"    → {len(raw_reddit)} relevant posts\n")
    if fetch_cmts:
        print("💬  Fetching Reddit comments…")
    reddit_posts = [reddit_extract(p, fetch_cmts=fetch_cmts) for p in raw_reddit]

    # ── Hacker News ──
    print(f"\n🔍  Hacker News (past {args.time})…")
    raw_hn = hn_fetch(time_filter=args.time, limit=30)
    if fetch_cmts:
        print("💬  Fetching HN comments…")
    hn_posts = [x for x in (hn_extract(h, fetch_cmts=fetch_cmts) for h in raw_hn) if x]
    hn_posts.sort(key=lambda p: p["score"], reverse=True)
    print(f"    → {len(hn_posts)} relevant stories")

    # ── GitHub ──
    print(f"\n🔍  GitHub Issues (past {args.time})…")
    raw_gh = github_fetch(time_filter=args.time, limit=30)
    gh_posts = [github_extract(i) for i in raw_gh]
    print(f"    → {len(gh_posts)} issues")

    # ── X / Twitter ──
    print(f"\n🔍  X/Twitter (past 7 days)…")
    raw_tweets, tweet_users = twitter_fetch(limit=100)
    twitter_posts = [twitter_extract(t, tweet_users) for t in raw_tweets]
    twitter_posts.sort(key=lambda p: p["score"], reverse=True)
    print(f"    → {len(twitter_posts)} tweets")

    if args.no_summarize:
        print("\n── Reddit ──")
        for p in reddit_posts:
            print(f"  [{p['score']:>5}]  {p['title'][:70]}")
        print("\n── Hacker News ──")
        for p in hn_posts:
            print(f"  [{p['score']:>5}]  {p['title'][:70]}")
        print("\n── GitHub ──")
        for p in gh_posts:
            print(f"  [👍{p['reactions']:>4}]  {p['title'][:70]}")
        print("\n── X/Twitter ──")
        for p in twitter_posts[:20]:
            print(f"  [❤️{p['likes']:>4}]  @{p['username']}: {p['title'][:60]}")
        return

    # ── Summarize ──
    reddit_summary = claude_summarize(
        build_prompt(reddit_posts, args.time, "Reddit"), "Reddit")
    hn_summary = claude_summarize(
        build_prompt(hn_posts, args.time, "Hacker News"), "Hacker News")
    github_summary = claude_summarize(
        build_github_prompt(gh_posts, args.time), "GitHub Issues")
    twitter_summary = claude_summarize(
        build_twitter_prompt(twitter_posts, args.time), "X/Twitter") if twitter_posts else ""
    overview_summary = claude_summarize(
        build_overview_prompt(reddit_summary, hn_summary, github_summary, twitter_summary, args.time), "Overview")

    print("\n" + "━" * 70)
    print(overview_summary)
    print("━" * 70)

    if args.publish:
        data_path = os.path.join(os.path.dirname(__file__), "website", "public", "data.json")
        payload = {
            "generated":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "time_filter": args.time,
            "overview": {
                "summary":         overview_summary,
                "reddit_count":    len(reddit_posts),
                "hn_count":        len(hn_posts),
                "gh_count":        len(gh_posts),
                "twitter_count":   len(twitter_posts),
            },
            "reddit": {
                "summary":    reddit_summary,
                "post_count": len(reddit_posts),
                "posts":      [to_publish_post(p) for p in reddit_posts],
            },
            "hn": {
                "summary":    hn_summary,
                "post_count": len(hn_posts),
                "posts":      [to_publish_post(p) for p in hn_posts],
            },
            "github": {
                "summary":    github_summary,
                "post_count": len(gh_posts),
                "posts": [{
                    "source":       p["source"],
                    "title":        p["title"],
                    "repo":         p["repo"],
                    "score":        p["reactions"],
                    "num_comments": p["num_comments"],
                    "url":          p["url"],
                    "date":         p["date"],
                    "state":        p["state"],
                    "labels":       p["labels"],
                } for p in gh_posts],
            },
            "twitter": {
                "summary":    twitter_summary,
                "post_count": len(twitter_posts),
                "posts": [{
                    "source":       p["source"],
                    "title":        p["title"],
                    "score":        p["likes"],
                    "likes":        p["likes"],
                    "retweets":     p["retweets"],
                    "num_comments": p["num_comments"],
                    "url":          p["url"],
                    "date":         p["date"],
                    "username":     p["username"],
                } for p in twitter_posts[:50]],
            },
        }
        with open(data_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\n🌐  Saved to website/public/data.json")
        print(f"    → cd website && vercel --prod")


if __name__ == "__main__":
    main()
