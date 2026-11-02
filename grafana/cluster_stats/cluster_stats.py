#!/usr/bin/env python3
"""Cluster usage statistics — how RationAI users use rationai-jobs-ns.

The big picture — job counts, durations, requested vs used resources, GPU
demand (which GPU types are most wanted) and GPU idling. One dashboard
PNG (sent to Slack) + HTML report (saved locally), stamped with the
collection date.

Usage:
    python3 cluster_stats.py --dry-run   # print summary, save PNG + HTML
    python3 cluster_stats.py            # send summary + dashboard to SLACK_CHANNEL
"""

import argparse
import io
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# --- config / secrets (env vars override the defaults) ---
GRAFANA_URL = os.environ.get(
    "GRAFANA_URL", "https://grafana.rationai.cloud.trusted.e-infra.cz"
)
DATASOURCE_UID = os.environ.get(
    "DATASOURCE_UID", "db8e0c91-7b8f-41a5-8d3e-575e40303c5a"
)
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")  # set via env or k8s Secret
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")  # set via env or k8s Secret
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0CBQDVD9PC")
NS = os.environ.get("NS", "rationai-jobs-ns")
WINDOW = os.environ.get("WINDOW", "30d")

GiB = 2**30

# --- queries (verified against the Board of Shame datasource) ---
QUERIES = {
    "CPU": f'(avg_over_time(sum by (namespace, pod) (node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{{namespace=~"{NS}", container!=""}})[{WINDOW}:1h])) '
    f'/ on(namespace, pod) (avg_over_time(sum by (namespace, pod) (kube_pod_container_resource_requests{{namespace=~"{NS}", resource="cpu"}})[{WINDOW}:1h]))',
    "RAM": f'(avg_over_time(sum by (namespace, pod) (container_memory_working_set_bytes{{namespace=~"{NS}", container!=""}})[{WINDOW}:1h])) '
    f'/ on(namespace, pod) (avg_over_time(sum by (namespace, pod) (kube_pod_container_resource_requests{{namespace=~"{NS}", resource="memory"}})[{WINDOW}:1h]))',
    "GPU util": f'avg by (namespace, pod) (avg_over_time(DCGM_FI_PROF_GR_ENGINE_ACTIVE{{namespace=~"{NS}", pod!="", pod!~"nvidia-dcgm-exporter.*"}}[{WINDOW}:1h]))',
    "GPU mem": f'avg by (namespace, pod) (avg_over_time(DCGM_FI_DEV_FB_USED{{namespace=~"{NS}", pod!="", pod!~"nvidia-dcgm-exporter.*"}}[{WINDOW}:1h])) '
    f'/ on(namespace, pod) (avg by (namespace, pod) (avg_over_time(DCGM_FI_DEV_FB_USED{{namespace=~"{NS}", pod!="", pod!~"nvidia-dcgm-exporter.*"}}[{WINDOW}:1h])) '
    f'+ avg by (namespace, pod) (avg_over_time(DCGM_FI_DEV_FB_FREE{{namespace=~"{NS}", pod!="", pod!~"nvidia-dcgm-exporter.*"}}[{WINDOW}:1h])))',
}
# pod lifetime in hours: last-seen-of-metrics minus pod-created (the Board of Shame's own formula)
Q_DURATION = (
    f'((max by (namespace, pod) (max_over_time(timestamp(container_memory_working_set_bytes{{namespace=~"{NS}", container!=""}})[{WINDOW}:10m]))) '
    f'- on(namespace, pod) (max by (namespace, pod) (last_over_time(kube_pod_created{{namespace=~"{NS}"}}[{WINDOW}])))) / 3600'
)
Q_JOB = f'count by (namespace, pod, created_by_name) (count_over_time((kube_pod_info{{namespace=~"{NS}", created_by_kind="Job"}})[{WINDOW}:1h]))'
Q_REQ_CPU = (
    f"max by (namespace, pod) (last_over_time(kube_pod_container_resource_requests"
    f'{{namespace=~"{NS}", resource="cpu"}}[{WINDOW}]))'
)
Q_REQ_MEM = (
    f"max by (namespace, pod) (last_over_time(kube_pod_container_resource_requests"
    f'{{namespace=~"{NS}", resource="memory"}}[{WINDOW}]))'
)
Q_REQ_GPU = (
    f"sum by (namespace, pod, resource) (last_over_time(kube_pod_container_resource_requests"
    f'{{namespace=~"{NS}", resource=~"nvidia.*"}}[{WINDOW}]))'
)
Q_NODE = (
    f"count by (namespace, pod, node) (last_over_time(kube_pod_info"
    f'{{namespace=~"{NS}"}}[{WINDOW}]))'
)
Q_NODE_GPU = f"count by (node, label_nvidia_com_gpu_product) (last_over_time(kube_node_labels[{WINDOW}]))"

# node label nvidia.com/gpu.product -> display name (cluster also has H200/B200/B300)
MODELS = {
    "A100": "A100",
    "A40": "A40",
    "H100": "H100",
    "H200": "H200",
    "B200": "B200",
    "B300": "B300",
    "P100": "Tesla P100",
}


def prom_query(expr):
    """Instant PromQL via Grafana proxy -> list of (labels_dict, value)."""
    url = (
        f"{GRAFANA_URL}/api/datasources/proxy/uid/{DATASOURCE_UID}/api/v1/query?"
        + urllib.parse.urlencode({"query": expr})
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {GRAFANA_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(
            f"Prometheus query failed ({e.code}): {e.read()[:500]!r}\nQuery: {expr}"
        )
    except urllib.error.URLError as e:
        sys.exit(
            f"Cannot reach Grafana ({e.reason}) — check GRAFANA_URL / network / VPN."
        )
    if data.get("status") != "success":
        sys.exit(f"Prometheus query error: {data}\nQuery: {expr}")
    return [(s["metric"], float(s["value"][1])) for s in data["data"]["result"]]


def slack_api(method, payload):
    """Form-encoded Slack Web API call with the bot token."""
    # Slack wants form-encoded bodies; list/dict params are JSON-encoded
    # strings inside the form (e.g. completeUploadExternal's files)
    form = {
        k: json.dumps(v) if isinstance(v, (list, dict)) else v
        for k, v in payload.items()
    }
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    r = json.load(urllib.request.urlopen(req, timeout=120))
    if not r.get("ok"):
        sys.exit(f"Slack {method} failed: {r}")
    return r


def slack_upload_file(png, filename, channel_id, comment=""):
    """Upload bytes to a channel via the Slack Web API v2 file flow."""
    r = slack_api(
        "files.getUploadURLExternal", {"filename": filename, "length": len(png)}
    )
    urllib.request.urlopen(
        urllib.request.Request(
            r["upload_url"],
            data=png,
            headers={"Content-Type": "application/octet-stream"},
        ),
        timeout=120,
    ).read()
    slack_api(
        "files.completeUploadExternal",
        {
            "files": [{"id": r["file_id"]}],
            "channel_id": channel_id,
            "initial_comment": comment,
        },
    )


def collect():
    """All metrics per pod key (namespace, pod)."""

    def by_pod(expr, val=lambda v: v):
        return {(m["namespace"], m["pod"]): val(v) for m, v in prom_query(expr)}

    durations = by_pod(Q_DURATION)
    jobs = {
        (m["namespace"], m["pod"]): m.get("created_by_name", "")
        for m, _ in prom_query(Q_JOB)
    }
    req_cpu = by_pod(Q_REQ_CPU)
    req_mem = by_pod(Q_REQ_MEM, lambda v: v / GiB)
    gpu = {}  # pod -> {nvidia resource name: slice count}
    for m, v in prom_query(Q_REQ_GPU):
        gpu.setdefault((m["namespace"], m["pod"]), {})[m["resource"]] = v
    nodes = {
        (m["namespace"], m["pod"]): m["node"]
        for m, _ in prom_query(Q_NODE)
        if m.get("node")
    }  # skip pending-pod series
    node_products = {
        m["node"]: m["label_nvidia_com_gpu_product"]
        for m, _ in prom_query(Q_NODE_GPU)
        if m.get("label_nvidia_com_gpu_product")
    }
    ratios = {
        name: by_pod(q) for name, q in QUERIES.items()
    }  # CPU, RAM, GPU util, GPU mem
    return durations, jobs, req_cpu, req_mem, gpu, ratios, nodes, node_products


def gpu_pretty(resource, node_product):
    """Request resource -> GPU type: whole GPU uses the node's model, MIG its slice."""
    if resource == "nvidia_com_gpu":
        for key, name in MODELS.items():
            if node_product and key in node_product:
                return name
        return node_product or "unknown GPU"
    return "mig-" + resource.removeprefix("nvidia_com_mig_").replace("_", ".")


def build_stats(durations, jobs, req_cpu, req_mem, gpu, ratios, nodes, node_products):
    """Everything the dashboard and summary need, precomputed."""
    s = {}
    s["n_pods"] = len(durations)
    s["pod_hours"] = sum(durations.values())
    s["n_jobs"] = len({jobs[p] for p in durations if p in jobs})  # only measured pods
    s["durations"] = list(durations.values())
    cpu_pods = [p for p in ratios["CPU"] if p in req_cpu and p in durations]
    s["req_cpu"] = [req_cpu[p] for p in cpu_pods]
    s["cpu_util"] = [
        min(ratios["CPU"][p], 2.0) for p in cpu_pods
    ]  # clip 200% (bursting)
    mem_pods = [p for p in ratios["RAM"] if p in req_mem and p in durations]
    s["req_mem"] = [req_mem[p] for p in mem_pods]
    s["mem_util"] = [min(ratios["RAM"][p], 1.5) for p in mem_pods]  # clip 150%
    gpu_pods = [p for p in gpu if p in ratios["GPU util"] and p in durations]
    s["gpu_util"] = [ratios["GPU util"][p] for p in gpu_pods]
    s["gpu_mem_util"] = [
        ratios["GPU mem"][p] for p in gpu_pods if p in ratios["GPU mem"]
    ]
    # demand per GPU type, in requested GPU-hours
    slice_hours = {}
    for p, res in gpu.items():
        h = durations.get(p)
        if not h:
            continue
        for name, cnt in res.items():
            pretty = gpu_pretty(name, node_products.get(nodes.get(p, "")))
            slice_hours[pretty] = slice_hours.get(pretty, 0) + cnt * h
    s["slice_hours"] = dict(sorted(slice_hours.items(), key=lambda kv: -kv[1]))
    # idling: share of GPU pod-hours on pods averaging < 10% util
    hrs = [(durations[p], ratios["GPU util"][p]) for p in gpu_pods]
    total = sum(h for h, _ in hrs)
    s["gpu_pod_hours"] = total
    s["gpu_idle_share"] = (sum(h for h, u in hrs if u < 0.10) / total) if total else 0
    s["gpu_bands"] = [
        (sum(h for h, u in hrs if u < 0.10), "< 10%"),
        (sum(h for h, u in hrs if 0.10 <= u < 0.50), "10–50%"),
        (sum(h for h, u in hrs if u >= 0.50), "≥ 50%"),
    ]
    return s


def make_figure(s, when):
    """The 3x3 dashboard figure (durations, requests, utilization, GPU)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update(
        {
            "axes.facecolor": "#f7f8fa",
            "axes.grid": True,
            "grid.color": "#d9dce1",
            "grid.linewidth": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.labelsize": 9,
            "axes.edgecolor": "#b9bec7",
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
        }
    )
    BLUE, TEAL = "#3b6fb6", "#2a9d8f"
    fig, axes = plt.subplots(3, 3, figsize=(15, 11))
    med = statistics.median

    def hist(
        ax, vals, bins, title, med_fmt, clip=None, xticks=None, xlabels=None, log=False
    ):
        ax.hist(
            [min(v, clip) for v in vals] if clip else vals,
            bins=bins,
            color=BLUE,
            edgecolor="white",
            linewidth=0.5,
        )
        ax.axvline(med(vals), color="#333", ls="--", lw=1.2)
        ax.set_title(f"{title}\nmedian: {med_fmt.format(med(vals))}")
        if log:
            ax.set_xscale("log")
        if xticks is not None:
            ax.set_xticks(xticks, xlabels)
        ax.set_ylabel("pods")

    hist(
        axes[0, 0],
        s["durations"],
        np.geomspace(0.05, 2000, 30),
        "Job duration",
        "{:.1f} h",
        clip=2000,
        log=True,
        xticks=[1 / 60, 1, 6, 24, 168, 720],
        xlabels=["1m", "1h", "6h", "1d", "1w", "30d"],
    )
    hist(axes[0, 1], s["req_cpu"], 30, "CPU request", "{:.0f} cores")
    hist(
        axes[0, 2],
        s["cpu_util"],
        np.linspace(0, 1, 11),
        "CPU utilization",
        "{:.0%} of request",
        clip=1.0,
        xticks=np.linspace(0, 1, 6),
        xlabels=["0%", "20%", "40%", "60%", "80%", "100%"],
    )
    hist(axes[1, 0], s["req_mem"], 30, "RAM request", "{:.0f} GiB")
    hist(
        axes[1, 1],
        s["mem_util"],
        np.linspace(0, 1, 11),
        "RAM utilization",
        "{:.0%} of request",
        clip=1.0,
        xticks=np.linspace(0, 1, 6),
        xlabels=["0%", "20%", "40%", "60%", "80%", "100%"],
    )

    # GPU demand per type (requested GPU-hours)
    ax = axes[1, 2]
    if s["slice_hours"]:
        names = list(s["slice_hours"])[::-1]
        vals = [s["slice_hours"][n] for n in names]
        ax.barh(names, vals, color=TEAL, edgecolor="white")
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:,.0f} h", va="center", fontsize=8)
        ax.set_xlim(0, max(vals) * 1.2)
        ax.tick_params(labelsize=8)
    else:
        ax.text(
            0.5, 0.5, "no GPU pods", ha="center", va="center", transform=ax.transAxes
        )
    ax.set_title("GPU demand — requested GPU-hours by type")

    if s["gpu_util"]:
        hist(
            axes[2, 0],
            s["gpu_util"],
            np.linspace(0, 1, 11),
            "GPU utilization",
            "{:.0%} busy",
            xticks=np.linspace(0, 1, 6),
            xlabels=["0%", "20%", "40%", "60%", "80%", "100%"],
        )
    else:
        axes[2, 0].set_title("GPU utilization — no data")
    if s["gpu_mem_util"]:
        hist(
            axes[2, 1],
            s["gpu_mem_util"],
            np.linspace(0, 1, 11),
            "GPU memory",
            "{:.0%} of allocated",
            xticks=np.linspace(0, 1, 6),
            xlabels=["0%", "20%", "40%", "60%", "80%", "100%"],
        )
    else:
        axes[2, 1].set_title("GPU memory — no data")

    # GPU idling: how much of allocated GPU pod-hours sits at which util
    ax = axes[2, 2]
    total = s["gpu_pod_hours"] or 1
    shares = [h / total for h, _ in s["gpu_bands"]]
    ax.bar(range(3), shares, color=["#c44e52", "#e8a33d", "#55a868"], edgecolor="white")
    for i, v in enumerate(shares):
        ax.text(
            i, v, f"{v:.0%}", ha="center", va="bottom", fontsize=10, fontweight="bold"
        )
    ax.set_xticks(range(3), [b[1] for b in s["gpu_bands"]])
    ax.set_ylim(0, (max(shares) if shares else 1) * 1.18)
    ax.set_title("Allocated GPU time by avg utilization")
    ax.set_ylabel("share of GPU pod-hours")

    fig.suptitle("RationAI cluster usage", fontsize=15, fontweight="bold", y=0.985)
    fig.text(
        0.5,
        0.938,
        f"{NS} · last {WINDOW} · {s['n_pods']} pods, "
        f"{s['n_jobs']} jobs · data collected {when}",
        ha="center",
        fontsize=10,
        color="#555",
    )
    fig.text(
        0.995,
        0.005,
        "generated by cluster_stats.py",
        ha="right",
        fontsize=7,
        color="#aaa",
    )
    fig.tight_layout(rect=[0, 0.01, 1, 0.925])
    return fig


def make_dashboard(s, when):
    """Dashboard as PNG bytes (for Slack)."""
    import matplotlib.pyplot as plt

    fig = make_figure(s, when)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


def make_html(s, when):
    """Standalone HTML report: stat cards + the dashboard as inline SVG."""
    import matplotlib.pyplot as plt

    fig = make_figure(s, when)
    svg = io.StringIO()
    fig.savefig(svg, format="svg")
    plt.close(fig)
    med = statistics.median
    cards = [
        (f"{s['n_pods']:,}", "job pods"),
        (f"{s['n_jobs']:,}", "unique jobs"),
        (f"{s['pod_hours']:,.0f}", "pod-hours total"),
        (f"{med(s['durations']):.1f} h", "median duration"),
        (f"{med(s['cpu_util']):.0%}", "median CPU util"),
        (f"{med(s['mem_util']):.0%}", "median RAM util"),
    ]
    if s["slice_hours"]:
        top, hrs = next(iter(s["slice_hours"].items()))
        cards.append((f"{hrs:,.0f} h", f"{top} — top GPU demand"))
    if s["gpu_util"]:
        cards.append((f"{s['gpu_idle_share']:.0%}", "GPU time under 10% busy"))
    cards_html = "".join(
        f'<div class="card"><div class="v">{v}</div><div class="l">{l}</div></div>'
        for v, l in cards
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>RationAI cluster usage</title>
<style>
 body {{ margin:0; padding:28px; background:#eef1f5; color:#222;
        font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif }}
 .wrap {{ max-width:1240px; margin:0 auto }}
 h1 {{ margin:0 0 4px; font-size:26px }}
 .sub {{ color:#666; margin-bottom:22px; font-size:14px }}
 .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
           gap:12px; margin-bottom:22px }}
 .card {{ background:#fff; border-radius:10px; padding:14px 16px;
          box-shadow:0 1px 3px rgba(0,0,0,.10) }}
 .card .v {{ font-size:22px; font-weight:700 }}
 .card .l {{ font-size:11.5px; color:#788; text-transform:uppercase;
             letter-spacing:.04em; margin-top:2px }}
 figure {{ margin:0; background:#fff; border-radius:10px; padding:18px;
           box-shadow:0 1px 3px rgba(0,0,0,.10) }}
 figure svg {{ width:100%; height:auto }}
 footer {{ color:#99a; font-size:12px; text-align:right; margin-top:10px }}
</style></head><body><div class="wrap">
<h1>RationAI cluster usage</h1>
<div class="sub">{NS} · last {WINDOW} · data collected {when}</div>
<div class="cards">{cards_html}</div>
<figure>{svg.getvalue()}</figure>
<footer>generated by cluster_stats.py</footer>
</div></body></html>"""


def format_summary(s, when):
    med = statistics.median
    lines = [
        f"*📊 Cluster usage stats — last {WINDOW}* ({NS})",
        f"_Data collected {when}_",
        f"• *{s['n_pods']}* pods from *{s['n_jobs']}* unique jobs, {s['pod_hours']:,.0f} pod-hours total",
        f"• median duration {med(s['durations']):.1f} h (max {max(s['durations']):,.0f} h)",
    ]
    if s["req_cpu"]:
        lines.append(
            f"• CPU: median request {med(s['req_cpu']):.0f} cores, "
            f"median utilization {med(s['cpu_util']):.0%} of request"
        )
    if s["req_mem"]:
        lines.append(
            f"• RAM: median request {med(s['req_mem']):.0f} GiB, "
            f"median utilization {med(s['mem_util']):.0%} of request"
        )
    if s["slice_hours"]:
        top, hrs = next(iter(s["slice_hours"].items()))
        lines.append(
            f"• GPU: most demanded type *{top}* ({hrs:,.0f} GPU-hours requested)"
        )
    if s["gpu_util"]:
        lines.append(
            f"• GPU pods: median util {med(s['gpu_util']):.0%}; "
            f"{s['gpu_idle_share']:.0%} of allocated GPU time sits < 10% busy"
        )
    return "\n".join(lines)


def selftest():
    durations = {("ns", "a"): 10, ("ns", "b"): 2, ("ns", "g"): 5, ("ns", "m"): 4}
    jobs = {("ns", "a"): "j1", ("ns", "b"): "j2", ("ns", "m"): "j3"}
    req_cpu = {("ns", "a"): 4, ("ns", "b"): 8, ("ns", "g"): 4, ("ns", "m"): 4}
    req_mem = {("ns", "a"): 16, ("ns", "b"): 8, ("ns", "g"): 16, ("ns", "m"): 8}
    gpu = {
        ("ns", "g"): {"nvidia_com_gpu": 2},
        ("ns", "m"): {"nvidia_com_mig_2g_20gb": 1},
    }
    ratios = {
        "CPU": {("ns", "a"): 0.5, ("ns", "b"): 0.2, ("ns", "g"): 0.9, ("ns", "m"): 0.3},
        "RAM": {("ns", "a"): 0.6, ("ns", "b"): 0.3, ("ns", "g"): 0.8, ("ns", "m"): 0.4},
        "GPU util": {("ns", "g"): 0.05, ("ns", "m"): 0.6},
        "GPU mem": {("ns", "g"): 0.4, ("ns", "m"): 0.5},
    }
    nodes = {("ns", "g"): "n1", ("ns", "m"): "n2"}
    node_products = {"n1": "NVIDIA-A100-SXM4-40GB", "n2": "NVIDIA-A100-80GB-PCIe"}
    s = build_stats(
        durations, jobs, req_cpu, req_mem, gpu, ratios, nodes, node_products
    )
    assert s["n_pods"] == 4 and s["n_jobs"] == 3 and s["pod_hours"] == 21, s
    assert s["slice_hours"] == {"A100": 10, "mig-2g.20gb": 4}, s["slice_hours"]
    assert s["gpu_idle_share"] == 5 / 9, s  # 5h <10% of 9 GPU-hours
    t = format_summary(s, "2025-01-01 10:00")
    assert "A100" in t and "2025-01-01" in t, t
    assert "mig-2g.20gb" in str(s["slice_hours"]), s["slice_hours"]
    png = make_dashboard(s, "2025-01-01 10:00")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
    html = make_html(s, "2025-01-01 10:00")
    assert "<svg" in html and "data collected 2025-01-01" in html and "card" in html
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--dry-run", action="store_true", help="print summary, save PNG, don't send"
    )
    ap.add_argument(
        "--selftest", action="store_true", help="run built-in checks and exit"
    )
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    when = f"{datetime.now():%Y-%m-%d %H:%M}"
    s = build_stats(*collect())
    png = make_dashboard(s, when)
    text = format_summary(s, when)
    html = make_html(s, when)
    open("cluster_stats.html", "w").write(html)
    if args.dry_run:
        print(text)
        open("cluster_stats.png", "wb").write(png)
        print("report: cluster_stats.html · chart: cluster_stats.png")
    else:
        if not SLACK_CHANNEL:
            sys.exit("SLACK_CHANNEL not set (or use --dry-run)")
        ch = SLACK_CHANNEL
        slack_api("chat.postMessage", {"channel": ch, "text": text})
        slack_upload_file(png, "cluster-stats.png", ch, "Cluster usage dashboard")
        print(f"Stats sent to channel {ch}.")


if __name__ == "__main__":
    main()
