import os
import random
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_from_directory

app = Flask(__name__)
load_dotenv()

GITHUB_API = "https://api.github.com"
DEFAULT_MAX_STARS = 400
REQUEST_TIMEOUT = 15


def default_form_values() -> dict:
    return {
        "repo": "",
        "count": 5,
        "reserve_count": 3,
        "contact_mode": "flag",
        "fake_mode": "flag",
        "min_age_days": 30,
        "min_repos": 1,
        "min_followers": 0,
        "follow_mode": "off",
        "follow_target": "",
        "follow_boost": 3.0,
        "show_all_table": False,
        "use_custom_token": False,
    }


def parse_repo(repo_input: str) -> tuple[str, str]:
    repo_input = repo_input.strip()
    if not repo_input:
        raise ValueError("Repository is required")
    if repo_input.startswith("http"):
        parsed = urlparse(repo_input)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 2:
            raise ValueError("Repo URL must look like https://github.com/owner/repo")
        owner, repo = parts[0], parts[1]
    else:
        parts = repo_input.split("/")
        if len(parts) != 2:
            raise ValueError("Repo must be in owner/repo format")
        owner, repo = parts
    return owner, repo


def github_headers(token_override: str | None = None) -> dict:
    token = token_override or os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def rate_limit_error(resp: requests.Response) -> RuntimeError:
    reset_epoch = resp.headers.get("X-RateLimit-Reset")
    if reset_epoch and reset_epoch.isdigit():
        reset_at = datetime.fromtimestamp(int(reset_epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return RuntimeError(f"GitHub API rate limit hit; set GITHUB_TOKEN env var. Reset at {reset_at}.")
    return RuntimeError("GitHub API rate limit hit; set GITHUB_TOKEN env var.")


def fetch_stargazers(
    owner: str,
    repo: str,
    max_records: int = DEFAULT_MAX_STARS,
    token_override: str | None = None,
    session: requests.Session | None = None,
) -> list[str]:
    logins: list[str] = []
    page = 1
    per_page = 100
    client = session or requests
    headers = github_headers(token_override)

    while len(logins) < max_records:
        params = {"per_page": per_page, "page": page}
        resp = client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/stargazers",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            raise ValueError("Repository not found or is private")
        if resp.status_code == 403:
            raise rate_limit_error(resp)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        logins.extend([item["login"] for item in batch])
        if len(batch) < per_page:
            break
        page += 1

    return logins[:max_records]


def fetch_user(login: str, token_override: str | None = None, session: requests.Session | None = None) -> dict:
    client = session or requests
    resp = client.get(
        f"{GITHUB_API}/users/{login}",
        headers=github_headers(token_override),
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 403:
        raise rate_limit_error(resp)
    resp.raise_for_status()
    return resp.json()


def check_follows_target(
    login: str,
    target: str,
    token_override: str | None = None,
    session: requests.Session | None = None,
) -> bool:
    client = session or requests
    resp = client.get(
        f"{GITHUB_API}/users/{login}/following/{target}",
        headers=github_headers(token_override),
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 204:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 403:
        raise rate_limit_error(resp)
    resp.raise_for_status()
    return False


def normalize_external_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme:
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return f"https://{url}"


def build_contact_links(user: dict) -> list[dict]:
    links = []
    blog = (user.get("blog") or "").strip()
    twitter = user.get("twitter_username")
    email = user.get("email")
    if blog:
        links.append({"label": "Website", "url": normalize_external_url(blog)})
    if twitter:
        links.append({"label": "X/Twitter", "url": f"https://twitter.com/{twitter}"})
    if email:
        links.append({"label": "Email", "url": f"mailto:{email}"})

    bio = (user.get("bio") or "").lower()
    contact_keywords = ["t.me/", "telegram", "linkedin.com", "linktr.ee", "discord.gg", "instagram.com"]
    for kw in contact_keywords:
        if kw in bio:
            links.append({"label": "Bio match", "url": None, "hint": kw})
            break
    return links


def has_contact(links: list[dict]) -> bool:
    return any(link.get("url") or link.get("hint") for link in links)


def is_fake(user: dict, min_age_days: int, min_repos: int, min_followers: int) -> bool:
    created_at = datetime.strptime(user["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created_at).days
    repos_ok = user.get("public_repos", 0) >= min_repos
    followers_ok = user.get("followers", 0) >= min_followers
    age_ok = age_days >= min_age_days
    return not (age_ok and repos_ok and followers_ok)


def summarize_user(user: dict) -> str:
    parts = []
    if user.get("company"):
        parts.append(user["company"])
    if user.get("location"):
        parts.append(user["location"])
    if user.get("bio"):
        parts.append(user["bio"])
    return " • ".join(parts)[:240]


def weighted_sample_without_replacement(users: list[dict], k: int) -> list[dict]:
    if k <= 0:
        return []

    pool = list(users)
    chosen: list[dict] = []

    while pool and len(chosen) < k:
        weights = [max(float(item.get("selection_weight", 1.0)), 0.0) for item in pool]
        total_weight = sum(weights)

        if total_weight <= 0:
            remaining = k - len(chosen)
            chosen.extend(random.sample(pool, k=min(remaining, len(pool))))
            break

        pick = random.uniform(0, total_weight)
        cumulative = 0.0
        picked_idx = 0
        for idx, weight in enumerate(weights):
            cumulative += weight
            if pick <= cumulative:
                picked_idx = idx
                break

        chosen.append(pool.pop(picked_idx))

    return chosen


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", form_values=default_form_values())


@app.route("/favicon.png", methods=["GET"])
def favicon_png():
    return send_from_directory(app.root_path, "favicon.png", mimetype="image/png")


@app.route("/favicon.ico", methods=["GET"])
def favicon_ico():
    return send_from_directory(app.root_path, "favicon.ico", mimetype="image/vnd.microsoft.icon")


@app.route("/pick", methods=["POST"])
def pick():
    form_values = default_form_values()

    try:
        owner, repo = parse_repo(request.form.get("repo", ""))
        form_values["repo"] = request.form.get("repo", "").strip()

        n = int(request.form.get("count", "0"))
        if n <= 0:
            raise ValueError("Count must be positive")
        form_values["count"] = n

        reserve_count = int(request.form.get("reserve_count", "0"))
        if reserve_count < 0:
            raise ValueError("Reserve count cannot be negative")
        form_values["reserve_count"] = reserve_count

        contact_mode = request.form.get("contact_mode", "flag")
        fake_mode = request.form.get("fake_mode", "flag")
        follow_mode = request.form.get("follow_mode", "off")
        if contact_mode not in {"flag", "require"}:
            raise ValueError("Invalid contact filter mode")
        if fake_mode not in {"flag", "exclude"}:
            raise ValueError("Invalid fake filter mode")
        if follow_mode not in {"off", "flag", "exclude", "boost"}:
            raise ValueError("Invalid follow filter mode")
        form_values["contact_mode"] = contact_mode
        form_values["fake_mode"] = fake_mode
        form_values["follow_mode"] = follow_mode

        min_age_days = int(request.form.get("min_age_days", 30))
        min_repos = int(request.form.get("min_repos", 1))
        min_followers = int(request.form.get("min_followers", 0))
        if min_age_days < 0 or min_repos < 0 or min_followers < 0:
            raise ValueError("Thresholds cannot be negative")
        form_values["min_age_days"] = min_age_days
        form_values["min_repos"] = min_repos
        form_values["min_followers"] = min_followers

        follow_target_raw = request.form.get("follow_target", "").strip().lstrip("@")
        follow_target = follow_target_raw or owner
        if "/" in follow_target or " " in follow_target:
            raise ValueError("Follow target must be a valid GitHub username")
        form_values["follow_target"] = follow_target_raw

        follow_boost = float(request.form.get("follow_boost", "3"))
        if follow_boost < 1:
            raise ValueError("Boost coefficient must be at least 1")
        form_values["follow_boost"] = follow_boost

        show_all_table = request.form.get("show_all_table") == "on"
        custom_token = request.form.get("custom_token", "").strip()
        use_custom_token = bool(custom_token)
        form_values["show_all_table"] = show_all_table
        form_values["use_custom_token"] = use_custom_token

        token_override = custom_token if use_custom_token else None

    except ValueError as exc:
        return render_template("index.html", error=str(exc), form_values=form_values), 400

    target_total = n + reserve_count

    try:
        with requests.Session() as session:
            stargazers = fetch_stargazers(owner, repo, token_override=token_override, session=session)
            if not stargazers:
                return render_template(
                    "index.html",
                    error="No stargazers found for this repository",
                    form_values=form_values,
                ), 400

            random.shuffle(stargazers)
            pool_candidates: list[dict] = []
            all_rows: list[dict] = []

            for login in stargazers:
                try:
                    data = fetch_user(login, token_override=token_override, session=session)
                except requests.RequestException:
                    continue

                contact_links = build_contact_links(data)
                fake = is_fake(data, min_age_days, min_repos, min_followers)
                missing_contact = not has_contact(contact_links)

                follows_target = None
                if follow_mode != "off":
                    try:
                        follows_target = check_follows_target(
                            login,
                            follow_target,
                            token_override=token_override,
                            session=session,
                        )
                    except requests.RequestException:
                        follows_target = False

                excluded_reasons = []
                if contact_mode == "require" and missing_contact:
                    excluded_reasons.append("No contact info")
                if fake_mode == "exclude" and fake:
                    excluded_reasons.append("Flagged suspicious")
                if follow_mode == "exclude" and follows_target is False:
                    excluded_reasons.append(f"Not following @{follow_target}")

                selection_weight = 1.0
                if follow_mode == "boost" and follows_target:
                    selection_weight = follow_boost

                row = {
                    "login": data["login"],
                    "name": data.get("name") or data.get("login"),
                    "avatar_url": data.get("avatar_url"),
                    "html_url": data.get("html_url"),
                    "public_repos": data.get("public_repos", 0),
                    "followers": data.get("followers", 0),
                    "created_at": data.get("created_at"),
                    "summary": summarize_user(data),
                    "contact_links": contact_links,
                    "missing_contact": missing_contact,
                    "fake": fake,
                    "follows_target": follows_target,
                    "selection_weight": selection_weight,
                    "included": len(excluded_reasons) == 0,
                    "filter_reason": ", ".join(excluded_reasons) if excluded_reasons else "Included",
                }

                if show_all_table:
                    all_rows.append(row)

                if row["included"]:
                    pool_candidates.append(row)

                if not show_all_table and len(pool_candidates) >= target_total:
                    break

    except (ValueError, RuntimeError, requests.RequestException) as exc:
        return render_template("index.html", error=str(exc), form_values=form_values), 400

    if len(pool_candidates) == 0:
        return render_template(
            "index.html",
            error="No users matched filters",
            form_values=form_values,
        ), 400

    draw_size = min(target_total, len(pool_candidates))
    if follow_mode == "boost":
        sampled = weighted_sample_without_replacement(pool_candidates, draw_size)
    else:
        sampled = random.sample(pool_candidates, k=draw_size)

    chosen_count = min(n, len(sampled))
    chosen = sampled[:chosen_count]
    reserve_pool = sampled[chosen_count : chosen_count + reserve_count]

    return render_template(
        "index.html",
        form_values=form_values,
        results=chosen,
        reserve_pool=reserve_pool,
        all_rows=all_rows if show_all_table else [],
        repo=f"{owner}/{repo}",
        count=n,
        reserve_count=reserve_count,
        filters={
            "contact_mode": contact_mode,
            "fake_mode": fake_mode,
            "follow_mode": follow_mode,
            "follow_target": follow_target,
            "follow_boost": follow_boost,
            "min_age_days": min_age_days,
            "min_repos": min_repos,
            "min_followers": min_followers,
            "show_all_table": show_all_table,
        },
        pool_total=len(pool_candidates),
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
