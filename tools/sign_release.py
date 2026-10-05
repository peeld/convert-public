"""Release PC only.  Key generation and signing for update zips (Ed25519).

    python tools/sign_release.py genkey <name>          writes <name>.key (private) and prints the public key as C++ bytes
    python tools/sign_release.py sign <key-file> <dist/files.json>    writes <dist/files.json>.sig

Make two pairs ("everyday" and "spare"). The private files never go in a repo or into
licenseConfig.h; only the two public keys do. Needs:  pip install cryptography
"""
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def raw_public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def main(argv):
    if len(argv) == 2 and argv[0] == "genkey":
        key = Ed25519PrivateKey.generate()
        path = Path(argv[1] + ".key")
        if path.exists():
            sys.exit("refusing to overwrite " + str(path))
        path.write_bytes(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                           serialization.NoEncryption()))
        pub = raw_public(key)
        print("private key:", path, "(keep it off the repo; back it up)")
        print("public key (hex):", pub.hex())
        print("C++: {" + ", ".join("0x%02x" % b for b in pub) + "}")
        return 0
    if len(argv) == 3 and argv[0] == "sign":
        key = Ed25519PrivateKey.from_private_bytes(Path(argv[1]).read_bytes())
        target = Path(argv[2])
        sig = key.sign(target.read_bytes())
        Path(str(target) + ".sig").write_bytes(sig)
        print("signed", target, "->", str(target) + ".sig")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
