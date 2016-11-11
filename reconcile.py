from local import config

from django.conf import settings
from django.core.mail import send_mail, EmailMessage

settings.configure(
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql_psycopg2',
            'NAME': config.NAME,
            'USER': config.USERNAME,
            'PASSWORD': config.PASSWORD,
            'HOST': config.HOST,
            'PORT': config.PORT,
        }
    },

    EMAIL_USE_TLS = True,
    EMAIL_HOST = '74.125.68.108',
    EMAIL_HOST_USER = config.SENDER,
    EMAIL_HOST_PASSWORD = config.PASSWORD_SENDER,
    EMAIL_PORT = 587,
    DEFAULT_FROM_EMAIL = config.SENDER,
    SERVER_EMAIL = config.SENDER,

    TIME_ZONE = 'Asia/Jakarta'
)
    

import os
import os.path
import sys
#sys.path.append(os.path.join('.', 'gen-py'))

from adm.models import Transaction
import parser

from datetime import datetime, time, timedelta
import logging
from logging.handlers import TimedRotatingFileHandler
import shutil

from redis import Redis
from rq_scheduler import Scheduler

# python-RQ Scheduler
scheduler = Scheduler(connection=Redis())

os.environ['TZ'] = 'Asia/Jakarta'

# LOGGER
LOG_FOLDER = '../log/reconcile'
if not os.path.exists(LOG_FOLDER):
    os.mkdir(LOG_FOLDER)

LOG_NAME = os.path.join(LOG_FOLDER, 'reconcile.log')
LOG_FORMAT = "%(asctime)s %(levelname)s - %(message)s"

logger = logging.getLogger("Rotating Log")
logger.setLevel(logging.INFO)

handler = TimedRotatingFileHandler(LOG_NAME, when="midnight")

formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
handler.setFormatter(formatter)
logger.addHandler(handler)

logging.basicConfig(filename=LOG_NAME, format=LOG_FORMAT,
                    level=logging.INFO, datefmt="%Y-%m-%d %I:%M:%S")

class Reconcile:
    #header_postpaid = 'DT|SWITCHERID|MERCHANT|REFNUM|SREFNUM|IDPEL|BL_TH|TRAN_AMOUNT|RP_TAG|RP_INSENTIF|VAT|RP_BK|BANKCODE'
    #header_prepaid = 'DT|SWITCHERID|MERCHANT|REFNUM|SREFNUM|METERNUM|TRAN_AMOUNT|ADMIN|RP_STAMPDUTY|RP_VAT|RP_PLT|RP_CPI|PP|PU|TOKEN|BANKCODE'
    #header_nontaglis = 'DT|SWITCHERID|MERCHANT|REFNUM|SREFNUM|IDPEL|REGNUM|DT_REGISTRATION|TRAN_CODE|TRAN_AMOUNT|BANKCODE'
    
    header_bpjs = 'TGL SETTLE | DATE TRX | NO REFF | NO VIRTUAL BPJSKS | NAMA PELANGGAN | KODE CABANG BPJS | AMOUNT | JUMLAH ANGGOTA VA | KODE CA'

    KODE_CA = '500208'

    RECONCILE_1 = 1
    RECONCILE_2 = 2 
    RECONCILE_3 = 3

    FRI = 4
    SAT = 5
    SUN = 6

    FTR_QUEUE = 'queue'
    FTR_LOCAL = 'ftr'
    FCN_LOCAL = 'fcn'

    BPJS_PRODUCT_CODE = '080003'

    FAIL = -1
    SUCCESS = 0

    def __init__(self):
        """Initiate to access parser service."""
        self.parser = parser.ParserImpl()

        self.ftr_bpjs = None

    def parse_bill_number(self, bill_number):
        """Returns product code and bill_number."""
        if '#' in bill_number:
            split = bill_number.split('#')
            product_code = int(split[0])
            if product_code == 1:
                product_code = 4
            bill_number = split[1]
        else:
            if len(bill_number) == 11: product_code = 2   # prepaid
            elif len(bill_number) == 12: product_code = 4 # postpaid
            elif len(bill_number) == 13: product_code = 3 # nontaglis
            else: return None
        return product_code, bill_number

    def add_zero_padding(self, n, length):
        """Return a number with zero left padding."""
        zero_padded = length - len(str(n))
        return zero_padded * '0' + str(n)

    def add_space_right_padding(self, n, length):
        """Return text with space right padding."""
        space_padded = length - len(str(n))
        return str(n) + space_padded * ' '

    def determine_reconcile_type(self):
        """ Return reconcile type.
            1 == Monday, Tuesday, Wednesday, Thursday
            2 == Friday, Saturday, Sunday
            3 == Holiday
        """
        now = datetime.now()

        if now.weekday() in [self.FRI, self.SAT, self.SUN]:
            return self.RECONCILE_2

        if now.date() in self.get_holidays():
            return self.RECONCILE_3

        return self.RECONCILE_1

    def get_holidays(self):
        """Return a list of holidays for current year."""
        holidays = list()

        filename = 'holiday-%s.txt' % datetime.now().year
        file_path = os.path.join('holiday', filename)

        with open(file_path) as f:
            for line in f.readlines():
                if line is None: continue
                if line == "\n": continue
                if line.startswith('#'): continue

                line = line.strip()
                if '#' in line:
                    spl = line.split('#')
                    line = spl[0].strip() 

                holiday = datetime.strptime(line, '%Y-%m-%d').date()
                holidays.append(holiday)
        return holidays

    def generate_ftr_ctl(self):
        """Generate ftr and ftr.ctl and store to list."""
        self.ftr_bpjs = list()

        self.ftr_bpjs.append(self.header_bpjs)

        # TODO filter based on date
        # this loop filters line for postpaid, prepaid, and nontaglis in a loop
        # for the performance purpose.
        try:
            today = datetime.now().date()
            yesterday = today - timedelta(days=1)

            # query all yesterday's transactions
            for i in Transaction.objects.filter(
                    product__internal_code__contains='BPJS',
                    timestamp__year=yesterday.year,
                    timestamp__month=yesterday.month,
                    timestamp__day=yesterday.day) \
                        .order_by('timestamp'):
                # status fail or pending is ignored
                if int(i.result_code) != 0: continue

                if int(i.timestamp.strftime('%H')) < 12: continue
                
                result = self.parser.parse_bit61_bpjs(i.product.biller.code,
                                                 self.BPJS_PRODUCT_CODE,
                                                 i.bill_number,
                                                 i.bit_61)

                if result is Exception: continue

                p = result['unstructured']
                
                line = '%s | %s | %s | %s | %s | %s | %s | %s | %s ' % (
                    today.strftime('%Y%m%d'),
                    i.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    str(p['Nomor Referensi'][0]),
                    i.bill_number,
                    p['Nama Pelanggan'][0],
                    p['Kode Cabang'][0],
                    int(i.amount),
                    p['Total Anggota VA'],
                    self.KODE_CA
                )
                
                self.ftr_bpjs.append(line)
            
            # query all today's transactions
            for i in Transaction.objects.filter(
                    product__internal_code__contains='BPJS',
                    timestamp__year=today.year,
                    timestamp__month=today.month,
                    timestamp__day=today.day) \
                        .order_by('timestamp'):
                # status fail or pending is ignored
                if int(i.result_code) != 0: continue
                
                if int(i.timestamp.strftime('%H')) >= 12 : continue

                result = self.parser.parse_bit61_bpjs(i.product.biller.code,
                                                 self.BPJS_PRODUCT_CODE,
                                                 i.bill_number,
                                                 i.bit_61)

                if result is Exception: continue

                p = result['unstructured']
                
                line = '%s | %s | %s | %s | %s | %s | %s | %s | %s ' % (
                    today.strftime('%Y%m%d'),
                    i.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    str(p['Nomor Referensi'][0]),
                    i.bill_number,
                    p['Nama Pelanggan'][0],
                    p['Kode Cabang'][0],
                    int(i.amount),
                    p['Total Anggota VA'],
                    self.KODE_CA
                )
                
                self.ftr_bpjs.append(line)

            logger.info("Query database success.")
        except IOError:
            logger.error("Fail to query database. Schedule task to restart.")
            scheduler.enqueue_in(timedelta(minutes=config.INTERVAL), self.main)
            sys.exit()

    def dump_ftr_ctl(self):
        """Write from memory to disk in QUEUE folder."""
        if not os.path.exists(self.FTR_QUEUE):
            os.mkdir(self.FTR_QUEUE)

        # create filename for postpaid
        now = datetime.now()
        ftr_bpjs_name = "%s_BPJSKS_%s.txt" % \
            (self.KODE_CA, now.strftime('%d%m%Y'))

        try:
            with open(os.path.join(self.FTR_QUEUE, ftr_bpjs_name), 'w') as f:
                for line in self.ftr_bpjs:
                    f.write(line + '\n')
                logger.info("Dump %s" % \
                    os.path.join(self.FTR_QUEUE, ftr_bpjs_name))

            logger.info("Dump success.")
        except:
            scheduler.enqueue_in(timedelta(minutes=config.INTERVAL), self.main)
            logger.error("Dumping to disk fails. Schedule task to restart.")
            sys.exit()

    def send_email(self):
        now = datetime.now()

        subject = 'BPJSKS_%s_%s' % (self.KODE_CA, now.strftime('%d%m%Y'))
        sender  = settings.EMAIL_HOST_USER
        pwd = settings.EMAIL_HOST_PASSWORD
        recepient = config.RECEPIENT
        message = config.MESSAGE
        
        file_bpjs_name = '%s_BPJSKS_%s.txt' % (self.KODE_CA, now.strftime('%d%m%Y'))
        src = os.path.join(self.FTR_QUEUE, file_bpjs_name)

        try:
            email = EmailMessage()
            email.subject = subject
            email.body = message
            email.from_email = sender
            email.to = [recepient]
            email.attach_file(src)
 
            email.send()
            logger.info("Sending email success.")
        except:
            scheduler.enqueue_in(timedelta(minutes=1), self.main)
            logger.error("Sending email fail. Schedule task to restart.")

    def move(self, src, dst):
        """Move an uploaded file from queue to ftr."""
        shutil.move(src, dst)
        logger.debug("Move %s to %s" % (src, dst))

    def main(self):
        # create a log folder for reconcile
        if not os.path.exists(LOG_FOLDER):
            os.mkdir(LOG_FOLDER)
        
        self.generate_ftr_ctl()
        self.dump_ftr_ctl()
        #self.send_email()
        #self.download()

        now = datetime.now()
        # step 2. CA upload ftr and ftr.ctl between 0:00 and 8:30
        if time(12, 0, 0) <= now.time() <= time(12, 15, 0):
             self.generate_ftr_ctl()
             self.dump_ftr_ctl()
             self.send_email()

if __name__ == '__main__':
    reconcile = Reconcile()
    reconcile.main()
