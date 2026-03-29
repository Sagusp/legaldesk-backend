"""
Helper script to promote a user to admin role.
Usage: python make_admin.py <email>
"""

import sys
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

async def make_admin(email: str):
    """Promote a user to admin role"""
    mongo_url = os.environ['MONGO_URL']
    client = AsyncIOMotorClient(mongo_url)
    db = client[os.environ['DB_NAME']]
    
    # Find user
    user = await db.users.find_one({"email": email})
    
    if not user:
        print(f"❌ User with email '{email}' not found")
        return False
    
    # Update to admin
    result = await db.users.update_one(
        {"email": email},
        {"$set": {"role": "admin"}}
    )
    
    if result.modified_count > 0:
        print(f"✅ User '{user['name']}' ({email}) is now an admin!")
        return True
    else:
        print(f"ℹ️  User '{user['name']}' ({email}) is already an admin")
        return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python make_admin.py <email>")
        print("Example: python make_admin.py admin@example.com")
        sys.exit(1)
    
    email = sys.argv[1]
    asyncio.run(make_admin(email))
