import json, subprocess, sys
tok = subprocess.run(["gh","auth","token"],capture_output=True,text=True).stdout.strip()
import urllib.request
def get(url, accept, bearer):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}", "Accept": accept})
    return json.load(urllib.request.urlopen(req))
t = json.load(urllib.request.urlopen(urllib.request.Request(
    f"https://ghcr.io/token?scope=repository:shkolnik-beep/{(sys.argv+['registry-probe'])[1]}:pull",
    headers={"Authorization": "Basic "+__import__("base64").b64encode(f"shkolnik-beep:{tok}".encode()).decode()})))["token"]
repo, tag = (sys.argv+["registry-probe","g20"])[1:3]
idx = get(f"https://ghcr.io/v2/shkolnik-beep/{repo}/manifests/{tag}","application/vnd.oci.image.index.v1+json",t)
dg = [m["digest"] for m in idx["manifests"] if m.get("platform",{}).get("os")=="linux"][0]
man = get(f"https://ghcr.io/v2/shkolnik-beep/{repo}/manifests/{dg}","application/vnd.oci.image.manifest.v1+json",t)
tot = 0
for l in man["layers"]:
    tot += l["size"]
    print(f'{l["size"]:>14,} bytes  {l["size"]/2**30:6.3f} GiB  {l["mediaType"].split(".")[-1]:>9}  {l["digest"][:26]}')
print(f'TOTAL {tot:,} bytes = {tot/2**30:.2f} GiB')
