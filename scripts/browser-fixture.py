"""Recreate only the isolated synthetic browser fixture; never touches var/ root data."""
import json
import secrets
from pathlib import Path
from bigbase.api import create_app

root=Path(__file__).resolve().parents[1]/'var'/'browser-test'
app=create_app(root,testing=True)
with app.state.store.transaction() as c:
    # Auth state only, in this dedicated fixture; keep audit and synthetic records.
    c.execute("DELETE FROM objects WHERE kind IN ('user','session','challenge','api_key')")
    password=secrets.token_urlsafe(24)
    u=app.state.security.create_user(c,'browser-test',password,'admin')
    payload={'username':u['username'],'password':password,'secret':app.state.security.secret(u)}
p=root/'credentials.json';p.write_text(json.dumps(payload));p.chmod(0o600)
print('Fixture de autenticação sintética preparada em var/browser-test.')
