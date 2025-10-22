sudo mkdir -p /opt/homelab-agent
sudo cp homelab_agent.py .env /opt/homelab-agent/
cd /opt/homelab-agent
python -m venv venv
./venv/bin/pip install psutil requests python-dotenv

sudo systemctl daemon-reload
sudo systemctl enable --now homelab-agent.service
sudo systemctl status homelab-agent.service
