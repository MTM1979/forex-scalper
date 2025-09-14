import time
import threading
import MetaTrader5 as mt5
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from flask import Flask, render_template, jsonify, request
import json
import datetime
import requests
from bs4 import BeautifulSoup
import pandas as pd
import numpy as np
import plotly.graph_objs as go
import plotly.utils
from threading import Lock
import logging
from logging.handlers import RotatingFileHandler

# Initialize Flask app
app = Flask(__name__)

# Global variables to store state
signals = []
trade_log = []
performance_metrics = {
    "win_rate": 0,
    "drawdown": 0,
    "exposure": 0,
    "total_trades": 0,
    "profit": 0
}
news_items = []
mt5_status = "Disconnected"
bot_status = "Stopped"

# Threading locks
data_lock = Lock()

# Configuration (would ideally come from a config file or database)
class Config:
    EXNESS_EMAIL = "mmandimika@gmail.com"
    EXNESS_PASSWORD = "your_password"  # Should be encrypted in production
    
    accounts = {
        "main": {
            "login": 161282252,
            "password": "your_mt5_password",  # Should be encrypted
            "server": "Exness-MT5Real21"
        },
        "backup": {
            "login": 211007383,
            "password": "your_other_mt5_password",  # Should be encrypted
            "server": "Exness-MT5Trial9"
        }
    }
    
    selected_account = "main"
    CHROME_DRIVER_PATH = "./chromedriver.exe"
    SCRAPE_INTERVAL = 1800  # 30 minutes
    NEWS_UPDATE_INTERVAL = 3600  # 1 hour
    FXSTREET_URL = "https://www.fxstreet.com/economic-calendar"
    
    # Strategy settings
    use_ml = False
    use_correlation = False
    use_multi_timeframe = True

# Initialize logging
def setup_logging():
    handler = RotatingFileHandler('forex_scalper.log', maxBytes=10000, backupCount=3)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

# Enhanced signal scraping function
def scrape_signals():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    
    signals_data = []
    
    try:
        driver = webdriver.Chrome(service=Service(Config.CHROME_DRIVER_PATH), options=options)
        driver.get("https://my.exness.global/pa/analytics/analystViews")
        time.sleep(3)
        
        # Login with explicit waits and better error handling
        try:
            email_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "email"))
            )
            email_field.send_keys(Config.EXNESS_EMAIL)
            
            password_field = driver.find_element(By.ID, "password")
            password_field.send_keys(Config.EXNESS_PASSWORD)
            
            login_button = driver.find_element(By.ID, "login-button")
            login_button.click()
            
            # Wait for page to load after login
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CLASS_NAME, "signal-card"))
            )
        except Exception as e:
            app.logger.error(f"Login failed: {e}")
            driver.quit()
            return signals_data
        
        # Scrape signals
        signal_cards = driver.find_elements(By.CLASS_NAME, "signal-card")
        
        for card in signal_cards:
            try:
                symbol = card.find_element(By.CLASS_NAME, "symbol").text
                direction = card.find_element(By.CLASS_NAME, "direction").text
                entry = float(card.find_element(By.CLASS_NAME, "entry").text)
                sl = float(card.find_element(By.CLASS_NAME, "sl").text)
                tp = float(card.find_element(By.CLASS_NAME, "tp").text)
                
                # Add timestamp
                timestamp = datetime.datetime.now().isoformat()
                
                signals_data.append({
                    "symbol": symbol,
                    "direction": direction,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "timestamp": timestamp
                })
            except Exception as e:
                app.logger.error(f"Error parsing signal card: {e}")
                continue
                
    except Exception as e:
        app.logger.error(f"Error in scrape_signals: {e}")
    finally:
        try:
            driver.quit()
        except:
            pass
            
    return signals_data

# Enhanced trade execution function
def execute_trade(signal):
    account = Config.accounts[Config.selected_account]
    result = {"success": False, "message": ""}
    
    try:
        if not mt5.initialize(login=account["login"], password=account["password"], server=account["server"]):
            error_msg = f"MT5 initialization failed: {mt5.last_error()}"
            app.logger.error(error_msg)
            result["message"] = error_msg
            return result
        
        symbol = signal["symbol"]
        direction = signal["direction"]
        entry = signal["entry"]
        sl = signal["sl"]
        tp = signal["tp"]
        lot = calculate_position_size(symbol, entry, sl)  # Dynamic position sizing
        
        action = mt5.ORDER_TYPE_BUY if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        symbol_info = mt5.symbol_info(symbol)
        
        if symbol_info is None:
            error_msg = f"Symbol {symbol} not found"
            app.logger.error(error_msg)
            result["message"] = error_msg
            mt5.shutdown()
            return result
        
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if action == mt5.ORDER_TYPE_BUY else tick.bid
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": action,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 5,
            "magic": 123456,
            "comment": "AutoTrade",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        trade_result = mt5.order_send(request)
        
        if trade_result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = f"Trade failed for {symbol}: {trade_result.retcode}"
            app.logger.error(error_msg)
            result["message"] = error_msg
        else:
            success_msg = f"Trade executed for {symbol} at {price}, SL: {sl}, TP: {tp}"
            app.logger.info(success_msg)
            result["success"] = True
            result["message"] = success_msg
            result["order_id"] = trade_result.order
            result["price"] = price
            
            # Log the trade
            log_trade(signal, trade_result, lot)
            
    except Exception as e:
        error_msg = f"Exception in execute_trade: {e}"
        app.logger.error(error_msg)
        result["message"] = error_msg
    finally:
        mt5.shutdown()
        
    return result

def calculate_position_size(symbol, entry, sl):
    # Simple risk management: risk 1% of account balance per trade
    account_info = mt5.account_info()
    if account_info is None:
        return 0.01  # Default lot size
    
    balance = account_info.balance
    risk_amount = balance * 0.01
    
    # Calculate pip value
    point = mt5.symbol_info(symbol).point
    pip_distance = abs(entry - sl) / point
    
    if pip_distance == 0:
        return 0.01  # Avoid division by zero
    
    # Calculate lot size
    lot_size = risk_amount / (pip_distance * 10)  # Simplified calculation
    
    # Round to acceptable lot size
    lot_size = round(lot_size, 2)
    
    # Ensure minimum and maximum lot sizes
    lot_size = max(0.01, min(lot_size, 50))
    
    return lot_size

def log_trade(signal, trade_result, lot_size):
    with data_lock:
        trade_log.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "symbol": signal["symbol"],
            "direction": signal["direction"],
            "entry_price": trade_result.price,
            "sl": signal["sl"],
            "tp": signal["tp"],
            "lot_size": lot_size,
            "order_id": trade_result.order,
            "profit": 0,  # Will be updated when position is closed
            "status": "open"
        })

def update_performance_metrics():
    with data_lock:
        if not trade_log:
            return
            
        # Calculate win rate
        closed_trades = [t for t in trade_log if t["status"] == "closed"]
        if closed_trades:
            winning_trades = [t for t in closed_trades if t["profit"] > 0]
            performance_metrics["win_rate"] = len(winning_trades) / len(closed_trades) * 100
        
        # Calculate drawdown
        equity = mt5.account_info().equity if mt5.initialize() else 0
        balance = mt5.account_info().balance if mt5.initialize() else 0
        if equity > 0 and balance > 0:
            performance_metrics["drawdown"] = (balance - equity) / balance * 100
        
        # Calculate exposure
        open_positions = mt5.positions_get() if mt5.initialize() else []
        exposure = sum(pos.volume for pos in open_positions)
        performance_metrics["exposure"] = exposure
        
        # Update other metrics
        performance_metrics["total_trades"] = len(trade_log)
        
        if mt5.initialize():
            performance_metrics["profit"] = mt5.account_info().profit
            mt5.shutdown()

def fetch_news():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(Config.FXSTREET_URL, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        news_data = []
        news_items = soup.find_all('div', class_='news-item')  # Update selector based on actual site structure
        
        for item in news_items[:10]:  # Get top 10 news items
            try:
                title = item.find('h3').text.strip()
                time_str = item.find('time').text.strip()
                summary = item.find('p').text.strip() if item.find('p') else ""
                
                news_data.append({
                    "title": title,
                    "time": time_str,
                    "summary": summary,
                    "impact": "High"  # Would need to parse actual impact level
                })
            except Exception as e:
                app.logger.error(f"Error parsing news item: {e}")
                continue
                
        return news_data
    except Exception as e:
        app.logger.error(f"Error fetching news: {e}")
        return []

def create_chart_data(symbol):
    # Fetch historical data for the symbol
    if not mt5.initialize():
        return None
        
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 100)
    mt5.shutdown()
    
    if rates is None:
        return None
        
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # Create candlestick chart
    candle = go.Candlestick(
        x=df['time'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name=symbol
    )
    
    # Find relevant signals for annotations
    symbol_signals = [s for s in signals if s['symbol'] == symbol]
    annotations = []
    
    for signal in symbol_signals:
        # Find the closest time in the data
        signal_time = pd.to_datetime(signal['timestamp'])
        idx = (df['time'] - signal_time).abs().idxmin()
        
        annotations.append(dict(
            x=df['time'][idx],
            y=df['close'][idx],
            xref='x',
            yref='y',
            text=signal['direction'],
            showarrow=True,
            arrowhead=2,
            ax=0,
            ay=-40,
            bgcolor='rgba(255, 255, 255, 0.8)',
            bordercolor='rgba(0, 0, 0, 0.8)'
        ))
    
    layout = go.Layout(
        title=f'{symbol} Price Chart',
        xaxis=dict(title='Time'),
        yaxis=dict(title='Price'),
        annotations=annotations
    )
    
    fig = go.Figure(data=[candle], layout=layout)
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

# Background tasks
def bot_loop():
    global bot_status, mt5_status, signals, news_items
    
    while bot_status == "Running":
        try:
            # Update MT5 status
            if mt5.initialize():
                mt5_status = "Connected"
                mt5.shutdown()
            else:
                mt5_status = "Disconnected"
            
            # Scrape signals
            new_signals = scrape_signals()
            with data_lock:
                signals = new_signals
            
            # Execute trades based on signals
            for signal in signals:
                if should_execute_trade(signal):  # Add filtering logic
                    result = execute_trade(signal)
                    app.logger.info(f"Trade execution result: {result}")
            
            # Update performance metrics
            update_performance_metrics()
            
            # Update news periodically
            if int(time.time()) % Config.NEWS_UPDATE_INTERVAL == 0:
                news_items = fetch_news()
            
            time.sleep(Config.SCRAPE_INTERVAL)
            
        except Exception as e:
            app.logger.error(f"Error in bot loop: {e}")
            time.sleep(60)  # Wait a minute before retrying

def should_execute_trade(signal):
    # Add filtering logic based on strategies
    if Config.use_multi_timeframe:
        # Check if signal confirms across multiple timeframes
        if not confirm_multi_timeframe(signal['symbol'], signal['direction']):
            return False
    
    if Config.use_correlation:
        # Check correlation with other symbols
        if not check_correlation(signal['symbol'], signal['direction']):
            return False
            
    # Add ML model prediction if enabled
    if Config.use_ml:
        if not ml_prediction(signal):
            return False
            
    return True

def confirm_multi_timeframe(symbol, direction):
    # Placeholder for multi-timeframe confirmation logic
    # Would check higher timeframes for confirmation of the signal
    return True  # Simplified for example

def check_correlation(symbol, direction):
    # Placeholder for correlation analysis
    # Would check correlated symbols for confirmation
    return True  # Simplified for example

def ml_prediction(signal):
    # Placeholder for ML model integration
    # Would use a trained model to predict signal success probability
    return True  # Simplified for example

# Flask routes
@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/signals')
def get_signals():
    with data_lock:
        return jsonify(signals)

@app.route('/api/trades')
def get_trades():
    with data_lock:
        return jsonify(trade_log)

@app.route('/api/performance')
def get_performance():
    with data_lock:
        return jsonify(performance_metrics)

@app.route('/api/news')
def get_news():
    with data_lock:
        return jsonify(news_items)

@app.route('/api/status')
def get_status():
    return jsonify({
        "bot_status": bot_status,
        "mt5_status": mt5_status,
        "account": Config.selected_account
    })

@app.route('/api/chart/<symbol>')
def get_chart(symbol):
    chart_data = create_chart_data(symbol)
    if chart_data:
        return chart_data
    return jsonify({"error": "Could not generate chart"})

@app.route('/api/control', methods=['POST'])
def control_bot():
    global bot_status
    
    action = request.json.get('action')
    
    if action == 'start':
        bot_status = "Running"
        # Start bot in a separate thread if not already running
        if not hasattr(control_bot, "bot_thread") or not control_bot.bot_thread.is_alive():
            control_bot.bot_thread = threading.Thread(target=bot_loop)
            control_bot.bot_thread.daemon = True
            control_bot.bot_thread.start()
        return jsonify({"status": "started"})
    
    elif action == 'stop':
        bot_status = "Stopped"
        return jsonify({"status": "stopped"})
    
    elif action == 'update_strategy':
        Config.use_ml = request.json.get('use_ml', Config.use_ml)
        Config.use_correlation = request.json.get('use_correlation', Config.use_correlation)
        Config.use_multi_timeframe = request.json.get('use_multi_timeframe', Config.use_multi_timeframe)
        return jsonify({"status": "strategy_updated"})
    
    elif action == 'switch_account':
        account = request.json.get('account')
        if account in Config.accounts:
            Config.selected_account = account
            return jsonify({"status": "account_switched"})
        return jsonify({"error": "Invalid account"}), 400
    
    return jsonify({"error": "Invalid action"}), 400

if __name__ == '__main__':
    setup_logging()
    app.run(debug=True, host='0.0.0.0', port=5000)