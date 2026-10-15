"""Lance mitmweb avec les options de l'add-on (/data/options.json)."""
import json
import os
import shlex

with open("/data/options.json", encoding="utf-8") as f:
    options = json.load(f)

confdir = "/data/mitmproxy"  # le certificat CA survit aux redémarrages
os.makedirs(confdir, exist_ok=True)

commande = [
    "mitmweb",
    "--mode", "regular",
    "--listen-host", "0.0.0.0",
    "--listen-port", "8080",
    "--web-host", "0.0.0.0",
    "--web-port", "8081",
    "--no-web-open-browser",
    "--set", f"confdir={confdir}",
    # Le trafic du réseau local arrive par la passerelle Docker : ne pas le bloquer.
    "--set", "block_global=false",
]

if options.get("sauvegarder", True):
    fichier = options.get("fichier") or "/share/mitmproxy/flows"
    os.makedirs(os.path.dirname(fichier), exist_ok=True)
    commande += ["--set", f"save_stream_file=+{fichier}"]  # « + » = ajouter au fichier existant

if options.get("mot_de_passe_web"):
    commande += ["--set", f"web_password={options['mot_de_passe_web']}"]

commande += shlex.split(options.get("arguments") or "")

print("Proxy mitmproxy :", " ".join(a if "web_password" not in a else "web_password=***" for a in commande), flush=True)
os.execvp(commande[0], commande)
