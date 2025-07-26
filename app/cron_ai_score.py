import orjson
import asyncio
import aiohttp
import aiofiles
import sqlite3
from datetime import datetime
from ml_models.score_model import ScorePredictor
import yfinance as yf
from collections import defaultdict
import pandas as pd
from tqdm import tqdm
import concurrent.futures
import re
from itertools import combinations
from dotenv import load_dotenv
import os
from utils.feature_engineering import *

import gc
#Enable automatic garbage collection
gc.enable()

load_dotenv()
api_key = os.getenv('FMP_API_KEY')


async def save_json(symbol, data):
    with open(f"json/ai-score/companies/{symbol}.json", 'wb') as file:
        file.write(orjson.dumps(data))

# Function to delete all files in a directory
def delete_files_in_directory(directory):
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Failed to delete {file_path}. Reason: {e}")

async def fetch_historical_price(ticker):
    url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}?from=1995-10-10&apikey={api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            # Check if the request was successful
            if response.status == 200:
                data = await response.json()
                # Extract historical price data
                historical_data = data.get('historical', [])
                # Convert to DataFrame
                df = pd.DataFrame(historical_data).reset_index(drop=True)
                # Reverse the DataFrame so that the past dates are first
                df = df.sort_values(by='date', ascending=True).reset_index(drop=True)
                return df
            else:
                raise Exception(f"Error fetching data: {response.status} {response.reason}")


async def download_data(ticker, con, start_date, end_date, skip_downloading, save_data):

    file_path = f"ml_models/training_data/ai-score/{ticker}.json"

    if os.path.exists(file_path):
        try:
            with open(file_path, 'rb') as file:
                return pd.DataFrame(orjson.loads(file.read()))
        except:
            return pd.DataFrame()
    elif skip_downloading == False:

        try:
            # Define paths to the statement files
            statements = [
                f"json/financial-statements/ratios/quarter/{ticker}.json",
                f"json/financial-statements/key-metrics/quarter/{ticker}.json",
                #f"json/financial-statements/cash-flow-statement/quarter/{ticker}.json",
                #f"json/financial-statements/income-statement/quarter/{ticker}.json",
                #f"json/financial-statements/balance-sheet-statement/quarter/{ticker}.json",
                f"json/financial-statements/income-statement-growth/quarter/{ticker}.json",
                f"json/financial-statements/balance-sheet-statement-growth/quarter/{ticker}.json",
                f"json/financial-statements/cash-flow-statement-growth/quarter/{ticker}.json",
                #f"json/financial-statements/owner-earnings/quarter/{ticker}.json",
            ]

            # Async loading and filtering
            ignore_keys = ["symbol", "reportedCurrency", "calendarYear", "fillingDate", "acceptedDate", "period", "cik", "link", "finalLink","pbRatio","ptbRatio","grahamNumber"]
            async def load_and_filter_json(path):
                async with aiofiles.open(path, 'r') as f:
                    data = orjson.loads(await f.read())
                return [{k: v for k, v in item.items() if k not in ignore_keys and int(item["date"][:4]) >= 2000} for item in data]

            # Load all files concurrently
            data = await asyncio.gather(*(load_and_filter_json(s) for s in statements))
            ratios, key_metrics, income_growth, balance_growth, cashflow_growth = data

            #Threshold of enough datapoints needed!
            if len(ratios) < 50:
                print(f'Not enough data points for {ticker}')
                return


            # Combine all the data
            combined_data = defaultdict(dict)

            # Merge the data based on 'date'
            for entries in zip(ratios,key_metrics, income_growth, balance_growth, cashflow_growth):
                for entry in entries:
                    try:
                        date = entry['date']
                        for key, value in entry.items():
                            if key not in combined_data[date]:
                                combined_data[date][key] = value
                    except:
                        pass

            combined_data = list(combined_data.values())

            # Download historical stock data using yfinance
            df = await fetch_historical_price(ticker)
            # Get the list of columns in df
            df_columns = df.columns
            df_stats = generate_statistical_features(df)
            df_ta = generate_ta_features(df)

            # Filter columns in df_stats and df_ta that are not in df
            # Drop unnecessary columns from df_stats and df_ta
            df_stats_filtered = df_stats.drop(columns=df_columns.intersection(df_stats.columns), errors='ignore')
            df_ta_filtered = df_ta.drop(columns=df_columns.intersection(df_ta.columns), errors='ignore')

            # Extract the column names for indicators
            ta_columns = df_ta_filtered.columns.tolist()
            stats_columns = df_stats_filtered.columns.tolist()

            # Concatenate df with the filtered df_stats and df_ta
            df = pd.concat([df, df_ta_filtered, df_stats_filtered], axis=1)
            # Set up a dictionary for faster lookup of close prices and columns by date
            df_dict = df.set_index('date').to_dict(orient='index')

            # Helper function to find closest date within max_attempts
            def find_closest_date(target_date, max_attempts=10):
                counter = 0
                while target_date not in df_dict and counter < max_attempts:
                    target_date = (pd.to_datetime(target_date) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                    counter += 1
                return target_date if target_date in df_dict else None

            # Match combined data entries with stock data
            for item in combined_data:
                target_date = item['date']
                closest_date = find_closest_date(target_date)

                # Skip if no matching date is found
                if not closest_date:
                    continue

                # Fetch data from the dictionary for the closest matching date
                data = df_dict[closest_date]

                # Add close price to the item
                item['price'] = round(data['close'], 2)

                # Dynamically add indicator values from ta_columns and stats_columns
                for column in ta_columns+stats_columns:
                    item[column] = data.get(column, None)

            # Sort the combined data by date
            combined_data = sorted(combined_data, key=lambda x: x['date'])
            # Convert combined data to a DataFrame and drop rows with NaN values
            df_combined = pd.DataFrame(combined_data)
            

            #nan_columns = df_combined.isna().sum()
            #print(nan_columns[nan_columns > 0])  # Show only columns with NaNs

            fundamental_columns = [
                'revenue', 'costOfRevenue', 'grossProfit', 'netIncome', 'operatingIncome', 'operatingExpenses',
                'researchAndDevelopmentExpenses', 'ebitda', 'freeCashFlow', 'incomeBeforeTax', 'incomeTaxExpense',
                'operatingCashFlow','cashAndCashEquivalents', 'totalEquity','otherCurrentLiabilities', 'totalCurrentLiabilities', 'totalDebt',
                'totalLiabilitiesAndStockholdersEquity', 'totalStockholdersEquity', 'totalInvestments','totalAssets',
            ]
            

            # Function to compute combinations within a group
            def compute_column_ratios(columns, df, new_columns):
                column_combinations = list(combinations(columns, 2))
                
                for num, denom in column_combinations:
                    with np.errstate(divide='ignore', invalid='ignore'):
                        # Compute ratio and reverse ratio safely
                        ratio = df[num] / df[denom]
                        reverse_ratio = df[denom] / df[num]

                    # Define column names for both ratios
                    column_name = f'{num}_to_{denom}'
                    reverse_column_name = f'{denom}_to_{num}'

                    # Assign values to new columns, handling invalid values
                    new_columns[column_name] = np.nan_to_num(ratio, nan=0, posinf=0, neginf=0)
                    new_columns[reverse_column_name] = np.nan_to_num(reverse_ratio, nan=0, posinf=0, neginf=0)

            # Create an empty dictionary for the new columns
            new_columns = {}

            # Compute combinations for each group of columns
            #compute_column_ratios(fundamental_columns, df_combined, new_columns)
            #compute_column_ratios(stats_columns, df_combined, new_columns)
            #compute_column_ratios(ta_columns, df_combined, new_columns)

            # Concatenate the new ratio columns with the original DataFrame
            df_combined = pd.concat([df_combined, pd.DataFrame(new_columns, index=df_combined.index)], axis=1)

            # Clean up and replace invalid values
            df_combined = df_combined.replace([np.inf, -np.inf], 0).dropna()

            # Create 'Target' column to indicate if the next price is higher than the current one
            df_combined['Target'] = ((df_combined['price'].shift(-1) - df_combined['price']) / df_combined['price'] > 0).astype(int)

            # Copy DataFrame and round float values
            df_copy = df_combined.copy().map(lambda x: round(x, 2) if isinstance(x, float) else x)

            # Save to a file if there are rows in the DataFrame
            if not df_copy.empty and save_data == True:
                with open(file_path, 'wb') as file:
                    file.write(orjson.dumps(df_copy.to_dict(orient='records')))

            return df_copy

        except Exception as e:
            print(e)
            pass


async def chunked_gather(tickers, con, start_date, end_date, skip_downloading, save_data, chunk_size=10):
    # Helper function to divide the tickers into chunks
    def chunks(lst, size):
        for i in range(0, len(lst), size):
            yield lst[i:i+size]
    
    results = []
    
    for chunk in chunks(tickers, chunk_size):
        # Create tasks for each chunk
        tasks = [download_data(ticker, con, start_date, end_date, skip_downloading, save_data) for ticker in chunk]
        # Await the results for the current chunk
        chunk_results = await asyncio.gather(*tasks)
        # Accumulate the results
        results.extend(chunk_results)
    
    return results



async def warm_start_training(tickers, con, skip_downloading, save_data):
    start_date = datetime(1995, 1, 1).strftime("%Y-%m-%d")
    end_date = datetime.today().strftime("%Y-%m-%d")
    test_size = 0.2

    dfs = await chunked_gather(tickers, con, start_date, end_date, skip_downloading, save_data, chunk_size=100)
    
    train_list = []
    test_list = []

    for df in dfs:
        try:
            # Split the data into training and testing sets
            split_size = int(len(df) * (1 - test_size))
            train_data = df.iloc[:split_size]
            test_data = df.iloc[split_size:]

            # Append train data for combined training
            train_list.append(train_data)
            test_list.append(test_data)
        except:
            pass

    # Concatenate all train data together
    df_train = pd.concat(train_list, ignore_index=True)
    df_test = pd.concat(test_list, ignore_index=True)

    # Shuffle the combined training data
    df_train = df_train.sample(frac=1, random_state=42).reset_index(drop=True)
    df_test = df_test.sample(frac=1, random_state=42).reset_index(drop=True)

    print('======Warm Start Train Set Datapoints======')
    print(len(df_train))
    
    predictor = ScorePredictor()
    selected_features = [col for col in df_train if col not in ['price', 'date', 'Target','fiscalYear']]
    
    predictor.warm_start_training(df_train[selected_features], df_train['Target'])
    predictor.evaluate_model(df_test)
    
    return predictor

async def fine_tune_and_evaluate(ticker, con, start_date, end_date, skip_downloading, save_data):
    try:
        df = await download_data(ticker, con, start_date, end_date, skip_downloading, save_data)
        if df is None or len(df) == 0:
            print(f"No data available for {ticker}")
            return
        
        test_size = 0.2
        split_size = int(len(df) * (1-test_size))
        train_data = df.iloc[:split_size]
        test_data = df.iloc[split_size:]
        #selected_features = [col for col in df.columns if col not in ['date','price','Target']]
        # Fine-tune the model
        predictor = ScorePredictor()
        #predictor.fine_tune_model(train_data[selected_features], train_data['Target'])
        print(f"Evaluating fine-tuned model for {ticker}")
        data = predictor.evaluate_model(test_data)

        
        if (data['accuracy'] < 100 and data['precision'] < 100 and
        data['f1_score'] >= 20 and data['recall_score'] >= 20) and len(data.get('backtest',[])) > 0:
            await save_json(ticker, data)
            data['backtest'] = [
                {'date': entry['date'], 'yTest': entry['y_test'], 'yPred': entry['y_pred'], 'score': entry['score']}
                for entry in data['backtest']
            ]
            print(data)
            print(f"Saved results for {ticker}")
        else:
            try:
                os.remove(f"json/ai-score/companies/{ticker}.json")
            except:
                pass
        
    except Exception as e:
        print(f"Error processing {ticker}: {e}")

    finally:
        gc.collect()  # Force the garbage collector to release unreferenced memory

async def run():
    train_mode = True  # Set this to False for fine-tuning and evaluation
    skip_downloading = False
    save_data = True
    delete_data = True
    if delete_data:
        delete_files_in_directory("ml_models/training_data/ai-score")

    con = sqlite3.connect('stocks.db')
    cursor = con.cursor()
    cursor.execute("PRAGMA journal_mode = wal")
    

    if train_mode:
        # Warm start training
        stock_symbols = cursor.execute("SELECT DISTINCT symbol FROM stocks WHERE marketCap >= 300E6 AND symbol NOT LIKE '%.%'") #list(set(['CB','LOW','PFE','RTX','DIS','MS','BHP','BAC','PG','BABA','ACN','TMO','LLY','XOM','JPM','UNH','COST','HD','ASML','BRK-A','BRK-B','CAT','TT','SAP','APH','CVS','NOG','DVN','COP','OXY','MRO','MU','AVGO','INTC','LRCX','PLD','AMT','JNJ','ACN','TSM','V','ORCL','MA','BAC','BA','NFLX','ADBE','IBM','GME','NKE','ANGO','PNW','SHEL','XOM','WMT','BUD','AMZN','PEP','AMD','NVDA','AWR','TM','AAPL','GOOGL','META','MSFT','LMT','TSLA','DOV','PG','KO']))
        stock_symbols = [row[0] for row in cursor.fetchall()]
        
        #Test Mode
        #stock_symbols = ['TSLA','AMD','LLY']
        
        print('Training for', len(stock_symbols))
        predictor = await warm_start_training(stock_symbols, con, skip_downloading, save_data)
    
    #else:
        # Evaluation for all stocks
        #cursor.execute("SELECT DISTINCT symbol FROM stocks WHERE marketCap >= 500E6 AND symbol NOT LIKE '%.%'")
        #stock_symbols = [row[0] for row in cursor.fetchall()]
        
        
        print(f"Total tickers for fine-tuning: {len(stock_symbols)}")
        start_date = datetime(1995, 1, 1).strftime("%Y-%m-%d")
        end_date = datetime.today().strftime("%Y-%m-%d")
        for ticker in tqdm(stock_symbols):
            await fine_tune_and_evaluate(ticker, con, start_date, end_date, skip_downloading, save_data)
        
    
    con.close()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as e:
        print(f"Main execution error: {e}")