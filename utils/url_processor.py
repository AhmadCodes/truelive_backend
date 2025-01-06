#%%
from urllib.parse import quote, urlparse, urlunparse
import urllib.parse
from urllib.parse import quote

def encode_rtsp_password(rtsp_url):
    # Split the URL into parts
    prefix, rest = rtsp_url.split('://')
    splitted_url = rest.split('@')
    
    user_info = splitted_url[:-1]
    user_info = "@".join(user_info)
    host_info = "@".join(splitted_url[-1:])
    
    # Split user_info into username and password
    username, password = user_info.split(':', 1)
    # print(f"un ecoded passowrd: {password}")
    # Encode the password
    encoded_password = urllib.parse.quote(password, safe='')
    # print(encoded_password)
    
    # Reconstruct the RTSP URL
    encoded_rtsp_url = f"{prefix}://{username}:{encoded_password}@{host_info}"
    
    return encoded_rtsp_url

# Example usage
rtsp_url = "rtsp://admin:shin@bet2015@431dekalb.vidliveus.com:554/Streaming/Channels/102"
encoded_rtsp_url = encode_rtsp_password(rtsp_url)
print(encoded_rtsp_url)

#%% Check if the output is as expected
expected_url = "rtsp://admin:shin%40bet2015@431dekalb.vidliveus.com:554/Streaming/Channels/102"
assert encoded_rtsp_url == expected_url, f"Expected {expected_url}, but got {encoded_rtsp_url}"
# %%
