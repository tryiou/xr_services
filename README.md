xcloud service to cache coin gecko tokens list, and gather/refresh 'usd' and 'usd_24h_vol' for each token, injecting timestamp at each refresh,
serve cached data to client requests.

expose two calls to xrproxy: \
/xrs/cg_coins_list  \
return list of supported tokens and associated ids \
/xrs/cg_coins_data ["token_id1", "token_id2", (...)]  \
return cached pricing in 'usd', 'usd_24h_vol' and 'timestamp' for each token_id 

```
# xcloud service for existing exr service node setup;
# INSTALL PROCEDURE:
cd ~/exrproxy-env
git clone https://github.com/tryiou/xr_services
cd xr_services
pip3 install PyYAML
python3 install_cg_proxy_xrs.py
cd ~/exrproxy-env
docker stop exrproxy-env-xr_proxy-1
./deploy
```

```
# USAGE:
# Retrieve coins ids with:
curl --location --request POST 'http://snode_url.org/xrs/cg_coins_list'

{
    "success": true,
    "reply": {
        "data": [
            #(...) PRUNED RESULT
            {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin"
            },
            {
                "id": "blocknet",
                "symbol": "block",
                "name": "Blocknet"
            }
        ],
        "timestamp": 1708352422.4790668
    }
}


# Retrieve coins ids data with:
curl --location 'http://exrproxy1.airdns.org:42114/xrs/cg_coins_data' \
--header 'Content-Type: application/json' \
--data '[
    "blocknet",
    "bitcoin"
]'

{
    "success": true,
    "reply": {
        "blocknet": {
            "usd": 0.04113826,
            "usd_24h_vol": 81.24801606145427,
            "timestamp": 1708353154.4305177
        },
        "bitcoin": {
            "usd": 52071,
            "usd_24h_vol": 18123595080.932835,
            "timestamp": 1708353154.4305177
        }
    }
}
