source venv/bin/activate
pip3 install -r requirements.txt         
cp .env.example .env                       


python3 login.py                           

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000