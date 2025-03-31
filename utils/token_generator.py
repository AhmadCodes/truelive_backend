import jwt
import datetime
import secrets
import os
from typing import Optional

# Secret Key - in production, load from environment variable or secure storage
SECRET_KEY = os.getenv('JWT_SECRET', 'your-secret-key')

def generate_token(pc_id: str, pc_name: str, role: str = "controller", manager_id: Optional[str] = None, expiry_hours: int = 24) -> tuple:
    """
    Generate a JWT token for PC authentication.
    
    Args:
        pc_id: Unique ID of the PC
        pc_name: Name of the PC
        role: Role of the PC ('manager' or 'controller')
        manager_id: ID of the manager PC if this is a controller
        expiry_hours: Token expiration in hours (default 24)
        
    Returns:
        tuple: (token, expiry_datetime_iso)
    """
    # Create expiration timestamp
    expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=expiry_hours)
    
    # Create payload
    payload = {
        "pc_id": pc_id,
        "pc_name": pc_name,
        "role": role,
        "exp": expiry,
        "iat": datetime.datetime.utcnow(),
    }
    
    # Add manager_id if this is a controller
    if role == "controller" and manager_id:
        payload["manager_id"] = manager_id
    
    # Generate token
    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    
    return token, expiry.isoformat()

def validate_token(token: str) -> dict:
    """
    Validate a JWT token and return the payload if valid.
    
    Args:
        token: JWT token string
        
    Returns:
        dict: Token payload or None if invalid
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return {"error": "Token has expired"}
    except jwt.InvalidTokenError:
        return {"error": "Invalid token"}