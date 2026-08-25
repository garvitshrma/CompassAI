import requests
from bs4 import BeautifulSoup
res = requests.get('https://openlake.in/', timeout=15)
soup = BeautifulSoup(res.text, 'html.parser')
for a in soup.find_all('a', href=True):
    href = a['href']
    txt = a.get_text(strip=True).lower()
    if any(kw in txt or kw in href.lower() for kw in ['past', 'community', 'tulsyan', 'slok', 'about']):
        print('TEXT:', a.get_text(strip=True), 'URL:', href)