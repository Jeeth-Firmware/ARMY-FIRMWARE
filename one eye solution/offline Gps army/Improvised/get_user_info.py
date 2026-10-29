"""User info retrieval — SQLite version."""
import db_helper


def get_user_info(field_name):
    """Get field from user_info table (auth_key from device table)"""
    try:
        # auth_key is in device table, not user_info
        if field_name == 'auth_key':
            row = db_helper.fetchone("SELECT auth_key FROM device LIMIT 1")
        else:
            row = db_helper.fetchone(
                f"SELECT {field_name} FROM user_info LIMIT 1")
        if row:
            return row[0]
        else:
            print(f"No {field_name} found.")
            return None
    except Exception as e:
        print("Error:", e)
        return None


# Example usage
phone_number = get_user_info('phone_number')
ACCESS_TOKEN = get_user_info('auth_key')

if phone_number:
    print(f"Phone Number: {phone_number}")

if ACCESS_TOKEN:
    print(f"Access Token: {ACCESS_TOKEN[:8]}...")
