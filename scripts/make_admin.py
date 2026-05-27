"""
Promote a Firebase user to admin role.

Usage:
    python -m scripts.make_admin <email>

Run from the backend root (so the relative imports resolve). The user
must already have signed up via the web app (creating an auth account
+ user doc). This script:
  * sets the user's Firestore user doc 'role' to 'admin'
  * sets the Firebase Auth custom claim {role: 'admin'}

After running, the user must sign out and sign back in for the new
custom claim to be picked up by their ID token.
"""
import sys

from app.core.firebase import get_auth, get_db, init_firebase


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.make_admin <email>")
        sys.exit(1)
    email = sys.argv[1]
    init_firebase()
    auth = get_auth()
    try:
        user = auth.get_user_by_email(email)
    except Exception as e:
        print(f"No Firebase Auth user with email {email}: {e}")
        sys.exit(2)

    db = get_db()
    db.collection("users").document(user.uid).set(
        {
            "uid": user.uid,
            "email": email,
            "role": "admin",
        },
        merge=True,
    )
    auth.set_custom_user_claims(user.uid, {"role": "admin"})
    print(f"Promoted {email} ({user.uid}) to admin.")
    print("User must sign out and sign back in for the new claim to apply.")


if __name__ == "__main__":
    main()
