from dataclasses import dataclass, field
from time import sleep, time

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import settings
from impf.alert import send_alert, read_code

import logging

from impf.constructors import browser_options
from impf.decorators import shadow_ban

logger = logging.getLogger(__name__)


@dataclass
class Browser:
    driver: webdriver = field(init=False)
    wait: WebDriverWait = field(init=False)
    location: str
    code: str = ''
    location_full: str = ''  # Helper variable for extracting full MVZ name
    keep_browser: bool = False  # Helper variable to indicate whether or not to keep browser open for reuse
    error_counter: int = 0  # Helper variable to avoid infinite loop
    logger: logger = field(init=False)  # Internal adapter-logger to add PLZ field

    def __post_init__(self):
        opts = browser_options()
        if settings.SELENIUM_PATH:
            self.driver = webdriver.Chrome(settings.SELENIUM_PATH, chrome_options=opts)
        else:
            self.driver = webdriver.Chrome(chrome_options=opts)
        self.driver.implicitly_wait(2.5)
        self.wait = WebDriverWait(self.driver, settings.WAIT_BROWSER_MAXIMUM)
        self.logger = settings.LocationAdapter(logger, {'location': self.location[:5]})

    def reinit(self, *args, **kwargs):
        """ Hacky Helper function - sorry """
        self.location = kwargs.get('location')
        self.code = kwargs.get('code')
        self.error_counter = 0
        self.location_full = ''
        self.logger = settings.LocationAdapter(logger, {'location': self.location[:5]})

    @property
    def in_waiting_room(self) -> bool:
        """ Momentan im Warteraum? """
        title = self.wait.until(EC.presence_of_element_located((By.XPATH, '//h1')))
        return title.text == 'Virtueller Warteraum des Impfterminservice'

    @property
    def server_id(self) -> str:
        """ Returns the server identifier we're connected to (001, 002, ...) """
        return self.driver.current_url[8:11]

    @property
    def has_vacancy(self) -> bool:
        """ Impfzentrum hat freie Termine """
        try:
            element = self.wait.until(
                EC.presence_of_element_located((By.XPATH, '//div[contains(@class, "alert-danger")]')))
            return not ('keine freien Termine' in element.text)
        except TimeoutException:
            return True

    @property
    def register_limit_reached(self) -> bool:
        """ Max. Anzahl an Nummern-Registrierungen erreicht """
        try:
            element = self.wait.until(
                EC.presence_of_element_located((By.XPATH, '//span[contains(text(), "Anfragelimit erreicht")]')))
            return bool(element)
        except TimeoutException:
            return False

    @property
    def code_valid(self) -> bool:
        """ Check ob Vermittlungscode von Server generell akzeptiert wird """
        try:
            element = self.driver.find_element_by_xpath('//div[contains(@class, "kv-alert-danger")]')
            return not ('Ungültiger Vermittlungscode' in element.text)
        except NoSuchElementException:
            return True

    @property
    def code_error(self) -> bool:
        """ Check ob Vermittlungscode an sich ok, aber auf Error gelaufen ist; bspw.
         wegen zu vielen Anfragen (429) """
        try:
            sleep(1.5)
            element = self.driver.find_element_by_xpath('//div[contains(@class, "kv-alert-danger")]')
            return 'unerwarteter Fehler' in element.text
        except NoSuchElementException:
            return False

    @property
    def loading_vacancy(self) -> bool:
        """ Prüft ob Verfügbarkeit noch geladen wird """
        try:
            element = self.driver.find_element_by_xpath('//div[contains(text(),"Bitte warten, wir suchen")]')
            return bool(element)
        except NoSuchElementException:
            return False

    @property
    def too_many_requests(self) -> bool:
        """ Checks if we're being blocked; unfortunately there is no better way, as
        Selenium doesn't allow us to check HTTP status codes in the Network tab """
        relevant = time() - 120
        sleep(1.5)  # give browser time to catch-up
        for log in self.driver.get_log('browser'):
            if log.get('level') == 'SEVERE' \
                    and log.get('source') == 'network' \
                    and (log.get('timestamp') / 1000) > relevant \
                    and '429' in log.get('message'):
                return True
        return False

    def cookie_popup(self) -> None:
        try:
            button = self.driver.find_element_by_xpath('//a[contains(text(),"Auswahl bestätigen")]')
            button.click()
        except:
            pass

    def page_ready(self) -> bool:
        """ Not in use; optional to check if page is ready (unreliable) before proceeding """
        page_state = self.driver.execute_script('return document.readyState;')
        return page_state == 'complete'

    def main_page(self) -> None:
        self.logger.info('Navigating to ImpfterminService')
        self.driver.get('https://www.impfterminservice.de/impftermine')
        elements = self.wait.until(EC.presence_of_all_elements_located((By.XPATH, '//span[@role="combobox"]')))
        title = self.driver.find_element_by_xpath('//h1')
        assert title.text == 'Buchen Sie die Termine für Ihre Corona-Schutzimpfung'
        self.cookie_popup()

        # Load Bundesländer
        elements[0].click()
        # Select BaWü
        element = self.wait.until(EC.presence_of_element_located(
            (By.XPATH, f'//li[@role="option" and contains(text() , "{settings.BUNDESLAND}")]')))
        element.click()
        self.logger.info(f'Selected Bundesland: {settings.BUNDESLAND}')

        # Load Cities
        elements[1].click()
        element = self.wait.until(EC.presence_of_element_located(
            (By.XPATH, f'//li[@role="option" and contains(text() , "{self.location}")]')))
        self.location_full = element.text
        element.click()
        self.logger.info(f'Selected Impfzentrum: {self.location_full}')

        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, f'//button[@type="submit"]')))
        submit.click()
        sleep(.5)

    def waiting_room(self):
        if not self.in_waiting_room: return
        self.logger.info('Taking a seat in the waiting room (very german)')
        while self.in_waiting_room: sleep(5)
        self.logger.info('No longer in waiting room!')

    @shadow_ban
    def location_page(self) -> None:
        title = self.wait.until(EC.presence_of_element_located((By.XPATH, '//h1')))
        assert title.text == 'Wurde Ihr Anspruch auf eine Corona-Schutzimpfung bereits geprüft?'
        self.cookie_popup()
        sleep(3)
        claim = 'Ja' if self.code else 'Nein'
        element = self.wait.until(EC.presence_of_element_located(
            (By.XPATH,
             f'//input[@type="radio" and @name="vaccination-approval-checked"]//following-sibling::span[contains(text(),"{claim}")]/..')))
        element.click()
        sleep(2)  # make the request gods happy

    def confirm_eligible(self) -> None:
        """ Termin verfügbar; prüfe ob Termine für unser Alter """
        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, f'//button[@type="submit"]')))
        submit.click()
        sleep(.5)
        element = self.wait.until(EC.presence_of_element_located(
            (By.XPATH,
             '//input[@type="radio" and @formcontrolname="isValid"]//following-sibling::span[contains(text(),"Ja")]/..')))
        element.click()

        # Enter Age
        input = self.wait.until(EC.presence_of_element_located((By.XPATH, '//input[@formcontrolname="age"]')))
        input.send_keys(str(settings.AGE))
        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, f'//button[@type="submit"]')))
        submit.click()

    def claim_code(self) -> None:
        """ Alles ok! fülle Felder aus um Vermittlungscode via SMS zu erhalten """
        title = self.wait.until(EC.presence_of_element_located((By.XPATH, '//h1')))
        assert title.text == 'Vermittlungscode anfordern'
        mail = self.wait.until(EC.presence_of_element_located((By.XPATH, '//input[@formcontrolname="email"]')))
        mail.send_keys(settings.MAIL)
        phone = self.wait.until(EC.presence_of_element_located((By.XPATH, '//input[@formcontrolname="phone"]')))
        phone.send_keys(settings.PHONE)
        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, f'//button[@type="submit"]')))
        submit.click()

    def enter_sms(self, sms_code: str) -> None:
        """ Gibt den SMS Code ins Formular ein """
        title = self.wait.until(EC.presence_of_element_located((By.XPATH, '//h1')))
        assert title.text == 'SMS Verifizierung'
        code = self.wait.until(EC.presence_of_element_located((By.XPATH, '//input[@formcontrolname="pin"]')))
        code.send_keys(sms_code)
        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, f'//button[@type="submit"]')))
        submit.click()

    def alert_sms(self) -> str:
        """ Benachrichtigung User - um entweder SMS Code via ext. Plattform (Zulip, ...)
        oder manuell einzugeben. Wartet max. 10 Minuten, und fährt dann fährt dann fort """
        self.logger.info('Enter SMS code! Waiting for user input.')
        send_alert(settings.ALERT_SMS.replace('{{ LOCATION }}', self.location_full))
        start = time()
        while (time() - start) < settings.WAIT_SMS_MANUAL:
            _code = read_code()
            if _code:
                self.logger.warning(f'Received Code from backend: {_code} - entering now...')
                send_alert(f'Entering code "{_code}"; check your mails! Thanks for using RAUSYS Technologies :)')
                return _code
            sleep(15)
        self.logger.warning('No SMS code received from backend')

    @shadow_ban
    def fill_code(self) -> None:
        """ Vermittlungscode für Location eingeben und prüfen """
        for i in range(3):
            element = self.wait.until(
                EC.presence_of_element_located((By.XPATH, f'//input[@type="text" and @data-index="{i}"]')))
            element.send_keys(self.code.split('-')[i])
        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, f'//button[@type="submit"]')))
        submit.click()

    def search_appointments(self) -> bool:
        """ Suche Termine mit Vermittlungscode """
        title = self.wait.until(EC.presence_of_element_located((By.XPATH, '//h1')))
        assert title.text == 'Onlinebuchung für Ihre Corona-Schutzimpfung'
        submit = self.wait.until(EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "Termine suchen")]')))
        submit.click()
        sleep(2.5)
        try:
            if self.driver.find_element_by_xpath('//span[@class="its-slot-pair-search-no-results"]') \
                    or self.driver.find_element_by_xpath(
                    '//span[contains(@class, "text-pre-wrap") and contains(text(), "Fehler")]'):
                self.logger.info('Vermittlungscode ok, but not free vaccination slots')
                return False
        except NoSuchElementException:
            pass

        element = self.driver.find_element_by_xpath('//*[contains(text(), "1. Impftermin")]')
        return bool(element)

    def wiggle_recover(self) -> None:
        """ Can solve `429` error by clicking Yes, No """
        claims = [
            'Nein' if self.code else 'Ja',  # click opposite than current
            'Ja' if self.code else 'Nein'
        ]
        for claim in claims:
            element = self.wait.until(EC.presence_of_element_located(
                (By.XPATH, f'//input[@type="radio" and @name="vaccination-approval-checked"]//following-sibling::span[contains(text(),"{claim}")]/..')))
            element.click()
            sleep(2)

    def rescan_appointments(self):
        """ Erneut im Buchungsbildschirm nach Terminen suchen """
        close = self.wait.until(EC.presence_of_all_elements_located((By.XPATH, '//button[contains(text(), "Abbrechen")]')))[-1]
        close.click()
        try:
            rescan = self.wait.until(EC.presence_of_element_located((By.XPATH, f'//a[contains(text(), "hier")]')))
            rescan.click()
            close = self.wait.until(EC.presence_of_all_elements_located((By.XPATH, '//button[contains(text(), "Abbrechen")]')))[-1]
            close.click()
        except TimeoutException:
            pass

    def alert_available(self):
        """ Alert, Termin verfügbar! Und Exit """
        self.logger.warning('Available appointments!')
        send_alert(settings.ALERT_AVAILABLE.replace('{{ LOCATION }}', self.location_full))
        sleep(settings.WAIT_SMS_MANUAL)
        self.keep_browser = True
        self.logger.warning('Exiting in 10 minutes, our job here is done. Keeping browser open.')
        sleep(600)
        exit()

    def control_main(self):
        """ Kontrollfunktion um Vermittlungscode zu beziehen """
        try:
            if self.error_counter == 5:
                self.logger.error('Maximum errors and retries exceeded - skipping location for now')
                return

            # Quick Restart
            if self.error_counter == 0: self.main_page()
            else: self.driver.refresh()

            self.logger.info(f'Connected to server [{self.server_id}]')
            self.waiting_room()
            self.location_page()
            if self.code: return self.control_appointment()

            while self.loading_vacancy: sleep(2.5)
            if not self.has_vacancy: self.logger.info('No vacancy right now...'); return
            self.confirm_eligible()
            if not self.has_vacancy: self.logger.info('No vacancy right now...'); return
            self.logger.warning(f'We have vacancy! Requesting Vermittlungscode for {self.driver.current_url}')
            self.claim_code()
            if self.register_limit_reached:
                self.logger.error('Request limit reached - try using a different phone number and email')
                send_alert(f'Server [{self.server_id}] returned max. requests. Consider changing phone number and email')
                return
            sms_code = self.alert_sms()
            self.enter_sms(sms_code)
            self.logger.info('Add the code you got via mail to settings.py and restart the script!')
        except StaleElementReferenceException:
            self.logger.warning('StaleElementReferenceException - we probably detatched somehow; reinitializing')
            # Reinitialize the browser, so we can reattach
            if self.keep_browser:
                self.driver.close()
                self.__post_init__()
        except:
            self.logger.exception('An unexpected exception occurred')
        finally:
            if not self.keep_browser: self.driver.close()

    def control_appointment(self):
        """ Kontrollfunktion um Verfügbarkeit von Impfterminen
         mit vorhandenem Vermittlungscode zu prüfen """
        self.fill_code()
        if not self.code_valid:
            self.logger.warning(f'Code invalid for server {self.driver.current_url}')
            self.logger.info('Retrying without code')
            self.code = ''
            return self.control_main()
        if self.code_error:
            # We're likely sending too many requests too quickly or the server is under too much stress
            self.logger.info(
                f'Ran into what is probably a temporary error with code {self.code}; retrying in '
                f'{settings.AVOID_SHADOW_BAN // 60}min')
            sleep(settings.AVOID_SHADOW_BAN)
            self.error_counter += 3
            return self.control_main()

        appointments = self.search_appointments()
        if settings.RESCAN_APPOINTMENT and not appointments:
            self.logger.info('RESCAN_APPOINTMENT is enabled - automatically rechecking in 10m...')
            while not appointments:
                sleep(150)  # Should be able to divide 600s (10m)
                self.logger.info('Rechecking for new appointments')
                self.rescan_appointments()
                appointments = self.search_appointments()

        if appointments:
            self.alert_available()
        else:
            self.logger.info('No appointments available right now :(')
