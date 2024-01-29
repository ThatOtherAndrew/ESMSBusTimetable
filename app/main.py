import asyncio
import csv
import email.parser
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import dateutil.parser
import quart

# noinspection PyPackageRequirements
import tabula

app = quart.Quart(__name__)
data_dir = Path('../data')


@contextmanager
def db_session() -> sqlite3.Connection:
    connection = sqlite3.connect(data_dir / 'timetable.db')
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@app.before_serving
def setup():
    print('Initialising data directory')
    data_dir.mkdir(exist_ok=True)
    (data_dir / 'email').mkdir(exist_ok=True)
    (data_dir / 'pdf').mkdir(exist_ok=True)
    (data_dir / 'csv').mkdir(exist_ok=True)

    print('Initialising database')
    with db_session() as connection:
        connection.execute('''
            CREATE TABLE IF NOT EXISTS timetable (
                departure_time INTEGER NOT NULL,
                vehicle TEXT NOT NULL,
                location TEXT NOT NULL,
                target_group TEXT NOT NULL,
                destination TEXT NOT NULL,
                comments TEXT,
                PRIMARY KEY (departure_time, vehicle, location, target_group, destination, comments)
            );
        ''')
        connection.commit()


@app.template_filter()
def format_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime('%a %d %H:%M')


@app.template_filter()
def relative_timestamp(timestamp: int) -> str:
    bus_time = datetime.fromtimestamp(timestamp)
    now = datetime.now()
    # now = datetime.fromtimestamp(1703083900)
    delta = bus_time - now

    if delta.days < 0:
        return 'missed'

    if bus_time.day > now.day:
        return 'tomorrow' if bus_time.day - now.day == 1 else f'in {delta.days} days'

    hours, seconds = divmod(delta.seconds, 3600)
    minutes = seconds // 60
    return (
        'in '
        f'{hours} hr{"s" if hours > 1 else ""}' if hours else ''
        f'{minutes} min' if minutes else ''
    )


@app.template_filter()
def location_colour(string: str) -> str:
    if 'SMC' in string:
        return '#e21737'
    if 'MES' in string:
        return '#1c3e93'

    return '#253745'


@app.template_filter()
def time_colour(timestamp: int) -> str:
    bus_time = datetime.fromtimestamp(timestamp)
    now = datetime.now()
    # now = datetime.fromtimestamp(1703083900)
    delta = bus_time - now

    if delta.days < 0 or delta.total_seconds() <= 60 * 5:
        return '#950f24'
    if delta.total_seconds() <= 60 * 15:
        return '#b7950b'
    if delta.total_seconds() <= 60 * 120:
        return '#39556b'
    if bus_time.day == now.day:
        return '#253745'

    return 'var(--muted-border-color)'


@app.route('/')
async def root() -> str:
    with db_session() as connection:
        timetable = connection.execute('''
            SELECT * FROM timetable
            WHERE departure_time >= ?
            ORDER BY departure_time ASC;
        ''', (
            datetime.now().timestamp(),
        )).fetchall()

    return await quart.render_template('index.html', timetable=timetable)


@app.route('/upload', methods=['POST'])
async def upload() -> str:
    print('Upload request received, begin processing data')

    data = await quart.request.get_data()
    # noinspection PyTypeChecker
    message: email.message.EmailMessage = email.parser.Parser(email.message.EmailMessage).parsestr(data.decode())
    timestamp = int(dateutil.parser.parse(message.get('Date')).timestamp())

    with (data_dir / f'email/{timestamp}.eml').open('wb') as file:
        file.write(data)

    print('Email saved')

    try:
        pdf = next(
            attachment for attachment in message.iter_attachments()
            if attachment.get_content_type() == 'application/pdf'
            and attachment.get_filename().startswith('Transport Schedule')
        )
    except StopIteration:
        error_message = 'Email received, no valid attachment found'
        print(error_message)
        return error_message

    pdf_path = data_dir / f'pdf/{timestamp}.pdf'
    with pdf_path.open('wb') as file:
        file.write(pdf.get_payload(decode=True))

    print('PDF attachment saved')

    csv_path = data_dir / f'csv/{timestamp}.csv'
    # noinspection PyTypeChecker
    await asyncio.to_thread(
        tabula.convert_into,
        input_path=pdf_path,
        output_path=csv_path.as_posix(),
        output_format='csv',
        pages='all'
    )

    print('PDF parsed and converted to CSV')

    with csv_path.open('r') as file:
        raw_csv = file.read()

    timestamp_length = 6
    day = datetime.strptime(
        next(filter(
            lambda string: string.isdigit() and len(string) == timestamp_length,
            pdf.get_filename().split()
        )),
        '%d%m%y'
    )
    day -= timedelta(days=day.weekday())
    chunks = filter(None, (chunk.strip() for chunk in re.split(r'^(?=Time)', raw_csv, flags=re.MULTILINE)))
    records = []
    for chunk in chunks:
        buffer = []
        for raw_row_data in csv.DictReader(StringIO(chunk)):
            raw_row_data: dict  # because PyCharm is silly
            buffer.append(raw_row_data)
            if not all(raw_row_data[key] for key in ('Time', 'Vehicle', 'From', 'Group', 'Destination')):
                continue

            row = {key: ' '.join(line[key] for line in buffer).replace('\n', ' ') for key in raw_row_data}
            buffer = []
            time = re.search(r'\d{4}', row['Time']).group()
            records.append((
                day.replace(hour=int(time[:2]), minute=int(time[2:])).timestamp(),
                row['Vehicle'],
                row['From'],
                row['Group'],
                row['Destination'],
                row['Comments']
            ))
        day += timedelta(days=1)

    with db_session() as connection:
        failed = 0
        for record in records:
            try:
                connection.execute('''
                    INSERT INTO timetable (
                        departure_time,
                        vehicle,
                        location,
                        target_group,
                        destination,
                        comments
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?
                    );
                ''', record)
            except sqlite3.IntegrityError:
                failed += 1

        connection.commit()

    response = f'{len(records)} bus record{"s" if len(records) != 1 else ""} added'
    if failed:
        response += f' (⚠️ {failed} invalid records ignored)'

    print(response)
    return response
