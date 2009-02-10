#!/usr/bin/env python2.5

"""Threaded IMAP4 client.

Based on RFC 2060 and original imaplib module.

Public classes:   IMAP4
                  IMAP4_SSL
                  IMAP4_stream

Public functions: Internaldate2Time
                  ParseFlags
                  Time2Internaldate
"""


__all__ = ("IMAP4", "IMAP4_SSL", "IMAP4_stream",
           "Internaldate2Time", "ParseFlags", "Time2Internaldate")

__version__ = "2.6"
__release__ = "2"
__revision__ = "6"
__credits__ = """
Authentication code contributed by Donn Cave <donn@u.washington.edu> June 1998.
String method conversion by ESR, February 2001.
GET/SETACL contributed by Anthony Baxter <anthony@interlink.com.au> April 2001.
IMAP4_SSL contributed by Tino Lange <Tino.Lange@isg.de> March 2002.
GET/SETQUOTA contributed by Andreas Zeidler <az@kreativkombinat.de> June 2002.
PROXYAUTH contributed by Rick Holbert <holbert.13@osu.edu> November 2002.
IDLE via threads suggested by Philippe Normand <phil@respyre.org> January 2005.
GET/SETANNOTATION contributed by Tomas Lindroos <skitta@abo.fi> June 2005.
New socket open code from http://www.python.org/doc/lib/socket-example.html."""
__author__ = "Piers Lauder <piers@janeelix.com>"
# Source URL: http://www.cs.usyd.edu.au/~piers/python/imaplib2

import binascii, os, Queue, random, re, select, socket, sys, time, threading

select_module = select

#       Globals

CRLF = '\r\n'
Debug = None                                    # Backward compatibility
IMAP4_PORT = 143
IMAP4_SSL_PORT = 993

IDLE_TIMEOUT_RESPONSE = '* IDLE TIMEOUT'
IDLE_TIMEOUT = 60*29                            # Don't stay in IDLE state longer

AllowedVersions = ('IMAP4REV1', 'IMAP4')        # Most recent first

#       Commands

CMD_VAL_STATES = 0
CMD_VAL_ASYNC = 1
NONAUTH, AUTH, SELECTED, LOGOUT = 'NONAUTH', 'AUTH', 'SELECTED', 'LOGOUT'

Commands = {
        # name            valid states             asynchronous
        'APPEND':       ((AUTH, SELECTED),            False),
        'AUTHENTICATE': ((NONAUTH,),                  False),
        'CAPABILITY':   ((NONAUTH, AUTH, SELECTED),   True),
        'CHECK':        ((SELECTED,),                 True),
        'CLOSE':        ((SELECTED,),                 False),
        'COPY':         ((SELECTED,),                 True),
        'CREATE':       ((AUTH, SELECTED),            True),
        'DELETE':       ((AUTH, SELECTED),            True),
        'DELETEACL':    ((AUTH, SELECTED),            True),
        'EXAMINE':      ((AUTH, SELECTED),            False),
        'EXPUNGE':      ((SELECTED,),                 True),
        'FETCH':        ((SELECTED,),                 True),
        'GETACL':       ((AUTH, SELECTED),            True),
        'GETANNOTATION':((AUTH, SELECTED),            True),
        'GETQUOTA':     ((AUTH, SELECTED),            True),
        'GETQUOTAROOT': ((AUTH, SELECTED),            True),
        'IDLE':         ((SELECTED,),                 False),
        'LIST':         ((AUTH, SELECTED),            True),
        'LOGIN':        ((NONAUTH,),                  False),
        'LOGOUT':       ((NONAUTH, AUTH, LOGOUT, SELECTED),   False),
        'LSUB':         ((AUTH, SELECTED),            True),
        'MYRIGHTS':     ((AUTH, SELECTED),            True),
        'NAMESPACE':    ((AUTH, SELECTED),            True),
        'NOOP':         ((NONAUTH, AUTH, SELECTED),   True),
        'PARTIAL':      ((SELECTED,),                 True),
        'PROXYAUTH':    ((AUTH,),                     False),
        'RENAME':       ((AUTH, SELECTED),            True),
        'SEARCH':       ((SELECTED,),                 True),
        'SELECT':       ((AUTH, SELECTED),            False),
        'SETACL':       ((AUTH, SELECTED),            False),
        'SETANNOTATION':((AUTH, SELECTED),            True),
        'SETQUOTA':     ((AUTH, SELECTED),            False),
        'SORT':         ((SELECTED,),                 True),
        'STATUS':       ((AUTH, SELECTED),            True),
        'STORE':        ((SELECTED,),                 True),
        'SUBSCRIBE':    ((AUTH, SELECTED),            False),
        'THREAD':       ((SELECTED,),                 True),
        'UID':          ((SELECTED,),                 True),
        'UNSUBSCRIBE':  ((AUTH, SELECTED),            False),
        }

UID_direct = ('SEARCH', 'SORT', 'THREAD')


def Int2AP(num):

    """string = Int2AP(num)
    Return 'num' converted to a string using characters from the set 'A'..'P'
    """

    val, a2p = [], 'ABCDEFGHIJKLMNOP'
    num = int(abs(num))
    while num:
        num, mod = divmod(num, 16)
        val.insert(0, a2p[mod])
    return ''.join(val)



class Request(object):

    """Private class to represent a request awaiting response."""

    def __init__(self, parent, name=None, callback=None, cb_arg=None):
        self.name = name
        self.callback = callback    # Function called to process result
        self.callback_arg = cb_arg  # Optional arg passed to "callback"

        self.tag = '%s%s' % (parent.tagpre, parent.tagnum)
        parent.tagnum += 1

        self.ready = threading.Event()
        self.response = None
        self.aborted = None
        self.data = None


    def abort(self, typ, val):
        self.aborted = (typ, val)
        self.deliver(None)


    def get_response(self, exc_fmt=None):
        self.callback = None
        self.ready.wait()

        if self.aborted is not None:
            typ, val = self.aborted
            if exc_fmt is None:
                exc_fmt = '%s - %%s' % typ
            raise typ(exc_fmt % str(val))

        return self.response


    def deliver(self, response):
        if self.callback is not None:
            self.callback((response, self.callback_arg, self.aborted))
            return

        self.response = response
        self.ready.set()




class IMAP4(object):

    """Threaded IMAP4 client class.

    Instantiate with:
        IMAP4(host=None, port=None, debug=None, debug_file=None)

        host       - host's name (default: localhost);
        port       - port number (default: standard IMAP4 port);
        debug      - debug level (default: 0 - no debug);
        debug_file - debug stream (default: sys.stderr).

    All IMAP4rev1 commands are supported by methods of the same name.

    Each command returns a tuple: (type, [data, ...]) where 'type'
    is usually 'OK' or 'NO', and 'data' is either the text from the
    tagged response, or untagged results from command. Each 'data' is
    either a string, or a tuple. If a tuple, then the first part is the
    header of the response, and the second part contains the data (ie:
    'literal' value).

    Errors raise the exception class <instance>.error("<reason>").
    IMAP4 server errors raise <instance>.abort("<reason>"), which is
    a sub-class of 'error'. Mailbox status changes from READ-WRITE to
    READ-ONLY raise the exception class <instance>.readonly("<reason>"),
    which is a sub-class of 'abort'.

    "error" exceptions imply a program error.
    "abort" exceptions imply the connection should be reset, and
            the command re-tried.
    "readonly" exceptions imply the command should be re-tried.

    All commands take two optional named arguments:
        'callback' and 'cb_arg'
    If 'callback' is provided then the command is asynchronous, so after
    the command is queued for transmission, the call returns immediately
    with the tuple (None, None).
    The result will be posted by invoking "callback" with one arg, a tuple:
        callback((result, cb_arg, None))
    or, if there was a problem:
        callback((None, cb_arg, (exception class, reason)))

    Otherwise the command is synchronous (waits for result). But note
    that state-changing commands will both block until previous commands
    have completed, and block subsequent commands until they have finished.

    All (non-callback) arguments to commands are converted to strings,
    except for AUTHENTICATE, and the last argument to APPEND which is
    passed as an IMAP4 literal.  If necessary (the string contains any
    non-printing characters or white-space and isn't enclosed with either
    parentheses or double quotes) each string is quoted.  However, the
    'password' argument to the LOGIN command is always quoted.  If you
    want to avoid having an argument string quoted (eg: the 'flags'
    argument to STORE) then enclose the string in parentheses (eg:
    "(\Deleted)").

    There is one instance variable, 'state', that is useful for tracking
    whether the client needs to login to the server. If it has the
    value "AUTH" after instantiating the class, then the connection
    is pre-authenticated (otherwise it will be "NONAUTH"). Selecting a
    mailbox changes the state to be "SELECTED", closing a mailbox changes
    back to "AUTH", and once the client has logged out, the state changes
    to "LOGOUT" and no further commands may be issued.

    Note: to use this module, you must read the RFCs pertaining to the
    IMAP4 protocol, as the semantics of the arguments to each IMAP4
    command are left to the invoker, not to mention the results. Also,
    most IMAP servers implement a sub-set of the commands available here.

    Note also that you must call logout() to shut down threads before
    discarding an instance.
    """

    class error(Exception): pass    # Logical errors - debug required
    class abort(error): pass        # Service errors - close and retry
    class readonly(abort): pass     # Mailbox status changed to READ-ONLY


    continuation_cre = re.compile(r'\+( (?P<data>.*))?')
    literal_cre = re.compile(r'.*{(?P<size>\d+)}$')
    mapCRLF_cre = re.compile(r'\r\n|\r|\n')
    mustquote_cre = re.compile(r"[^\w!#$%&'*+,.:;<=>?^`|~-]")
    response_code_cre = re.compile(r'\[(?P<type>[A-Z-]+)( (?P<data>[^\]]*))?\]')
    untagged_response_cre = re.compile(r'\* (?P<type>[A-Z-]+)( (?P<data>.*))?')
    untagged_status_cre = re.compile(r'\* (?P<data>\d+) (?P<type>[A-Z-]+)( (?P<data2>.*))?')


    def __init__(self, host=None, port=None, debug=None, debug_file=None):

        self.state = NONAUTH            # IMAP4 protocol state
        self.literal = None             # A literal argument to a command
        self.tagged_commands = {}       # Tagged commands awaiting response
        self.untagged_responses = {}    # {typ: [data, ...], ...}
        self.is_readonly = False        # READ-ONLY desired state
        self.idle_rqb = None            # Server IDLE Request - see _IdleCont
        self.idle_timeout = None        # Must prod server occasionally

        self._expecting_data = 0        # Expecting message data
        self._accumulated_data = []     # Message data accumulated so far
        self._literal_expected = None   # Message data descriptor

        # Create unique tag for this session,
        # and compile tagged response matcher.

        self.tagnum = 0
        self.tagpre = Int2AP(random.randint(4096, 65535))
        self.tagre = re.compile(r'(?P<tag>'
                        + self.tagpre
                        + r'\d+) (?P<type>[A-Z]+) (?P<data>.*)')

        if __debug__: self._init_debug(debug, debug_file)

        # Open socket to server.

        self.open(host, port)

        if __debug__:
            if debug:
                self._mesg('connected to %s on port %s' % (self.host, self.port))

        # Threading

        self.Terminate = False

        self.state_change_free = threading.Event()
        self.state_change_pending = threading.Lock()
        self.commands_lock = threading.Lock()

        self.ouq = Queue.Queue(10)
        self.inq = Queue.Queue()

        self.wrth = threading.Thread(target=self._writer)
        self.wrth.start()
        self.rdth = threading.Thread(target=self._reader)
        self.rdth.start()
        self.inth = threading.Thread(target=self._handler)
        self.inth.start()

        # Get server welcome message,
        # request and store CAPABILITY response.

        try:
            self.welcome = self._request_push(tag='continuation').get_response('IMAP4 protocol error: %s')[1]

            if 'PREAUTH' in self.untagged_responses:
                self.state = AUTH
                if __debug__: self._log(1, 'state => AUTH')
            elif 'OK' in self.untagged_responses:
                if __debug__: self._log(1, 'state => NONAUTH')
            else:
                raise self.error(self.welcome)

            typ, dat = self.capability()
            if dat == [None]:
                raise self.error('no CAPABILITY response from server')
            self.capabilities = tuple(dat[-1].upper().split())
            if __debug__: self._log(3, 'CAPABILITY: %r' % (self.capabilities,))

            for version in AllowedVersions:
                if not version in self.capabilities:
                    continue
                self.PROTOCOL_VERSION = version
                break
            else:
                raise self.error('server not IMAP4 compliant')
        except:
            self._close_threads()
            raise


    def __getattr__(self, attr):
        # Allow UPPERCASE variants of IMAP4 command methods.
        if attr in Commands:
            return getattr(self, attr.lower())
        raise AttributeError("Unknown IMAP4 command: '%s'" % attr)



    #       Overridable methods


    def open(self, host=None, port=None):
        """open(host=None, port=None)
        Setup connection to remote server on "host:port"
            (default: localhost:standard IMAP4 port).
        This connection will be used by the routines:
            read, send, shutdown, socket."""

        self.host = host is not None and host or ''
        self.port = port is not None and port or IMAP4_PORT
        self.sock = self.open_socket()
        self.read_fd = self.sock.fileno()


    def open_socket(self):
        """Open socket choosing first address family available."""

        msg = (-1, 'could not open socket')
        for res in socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            try:
                s = socket.socket(af, socktype, proto)
            except socket.error, msg:
                continue
            try:
                s.connect(sa)
            except socket.error, msg:
                s.close()
                continue
            break
        else:
            raise socket.error(msg)

        return s


    def read(self, size):
        """data = read(size)
        Read at most 'size' bytes from remote."""

        return self.sock.recv(size)


    def send(self, data):
        """send(data)
        Send 'data' to remote."""

        self.sock.sendall(data)


    def shutdown(self):
        """shutdown()
        Close I/O established in "open"."""

        self.sock.close()


    def socket(self):
        """socket = socket()
        Return socket instance used to connect to IMAP4 server."""

        return self.sock



    #       Utility methods


    def recent(self, **kw):
        """(typ, [data]) = recent()
        Return most recent 'RECENT' responses if any exist,
        else prompt server for an update using the 'NOOP' command.
        'data' is None if no new messages,
        else list of RECENT responses, most recent last."""

        name = 'RECENT'
        typ, dat = self._untagged_response('OK', [None], name)
        if dat[-1]:
            return self._deliver_dat(typ, dat, kw)
        kw['untagged_response'] = name
        return self.noop(**kw)  # Prod server for response


    def response(self, code, **kw):
        """(code, [data]) = response(code)
        Return data for response 'code' if received, or None.
        Old value for response 'code' is cleared."""

        typ, dat = self._untagged_response(code, [None], code.upper())
        return self._deliver_dat(typ, dat, kw)
        



    #       IMAP4 commands


    def append(self, mailbox, flags, date_time, message, **kw):
        """(typ, [data]) = append(mailbox, flags, date_time, message)
        Append message to named mailbox.
        All args except `message' can be None."""

        name = 'APPEND'
        if not mailbox:
            mailbox = 'INBOX'
        if flags:
            if (flags[0],flags[-1]) != ('(',')'):
                flags = '(%s)' % flags
        else:
            flags = None
        if date_time:
            date_time = Time2Internaldate(date_time)
        else:
            date_time = None
        self.literal = self.mapCRLF_cre.sub(CRLF, message)
        try:
            return self._simple_command(name, mailbox, flags, date_time, **kw)
        finally:
            self.state_change_pending.release()


    def authenticate(self, mechanism, authobject, **kw):
        """(typ, [data]) = authenticate(mechanism, authobject)
        Authenticate command - requires response processing.

        'mechanism' specifies which authentication mechanism is to
        be used - it must appear in <instance>.capabilities in the
        form AUTH=<mechanism>.

        'authobject' must be a callable object:

                data = authobject(response)

        It will be called to process server continuation responses.
        It should return data that will be encoded and sent to server.
        It should return None if the client abort response '*' should
        be sent instead."""

        self.literal = _Authenticator(authobject).process
        try:
            typ, dat = self._simple_command('AUTHENTICATE', mechanism.upper())
            if typ != 'OK':
                self._deliver_exc(self.error, dat[-1])
            self.state = AUTH
            if __debug__: self._log(1, 'state => AUTH')
        finally:
            self.state_change_pending.release()
        return self._deliver_dat(typ, dat, kw)


    def capability(self, **kw):
        """(typ, [data]) = capability()
        Fetch capabilities list from server."""

        name = 'CAPABILITY'
        kw['untagged_response'] = name
        return self._simple_command(name, **kw)


    def check(self, **kw):
        """(typ, [data]) = check()
        Checkpoint mailbox on server."""

        return self._simple_command('CHECK', **kw)


    def close(self, **kw):
        """(typ, [data]) = close()
        Close currently selected mailbox.

        Deleted messages are removed from writable mailbox.
        This is the recommended command before 'LOGOUT'."""

        if self.state != 'SELECTED':
            raise self.error('No mailbox selected.')
        try:
            typ, dat = self._simple_command('CLOSE')
        finally:
            self.state = AUTH
            if __debug__: self._log(1, 'state => AUTH')
            self.state_change_pending.release()
        return self._deliver_dat(typ, dat, kw)


    def copy(self, message_set, new_mailbox, **kw):
        """(typ, [data]) = copy(message_set, new_mailbox)
        Copy 'message_set' messages onto end of 'new_mailbox'."""

        return self._simple_command('COPY', message_set, new_mailbox, **kw)


    def create(self, mailbox, **kw):
        """(typ, [data]) = create(mailbox)
        Create new mailbox."""

        return self._simple_command('CREATE', mailbox, **kw)


    def delete(self, mailbox, **kw):
        """(typ, [data]) = delete(mailbox)
        Delete old mailbox."""

        return self._simple_command('DELETE', mailbox, **kw)


    def deleteacl(self, mailbox, who, **kw):
        """(typ, [data]) = deleteacl(mailbox, who)
        Delete the ACLs (remove any rights) set for who on mailbox."""

        return self._simple_command('DELETEACL', mailbox, who, **kw)


    def examine(self, mailbox='INBOX', **kw):
        """(typ, [data]) = examine(mailbox='INBOX', readonly=False)
        Select a mailbox for READ-ONLY access. (Flushes all untagged responses.)
        'data' is count of messages in mailbox ('EXISTS' response).
        Mandated responses are ('FLAGS', 'EXISTS', 'RECENT', 'UIDVALIDITY'), so
        other responses should be obtained via "response('FLAGS')" etc."""

        return self.select(mailbox=mailbox, readonly=True, **kw)


    def expunge(self, **kw):
        """(typ, [data]) = expunge()
        Permanently remove deleted items from selected mailbox.
        Generates 'EXPUNGE' response for each deleted message.
        'data' is list of 'EXPUNGE'd message numbers in order received."""

        name = 'EXPUNGE'
        kw['untagged_response'] = name
        return self._simple_command(name, **kw)


    def fetch(self, message_set, message_parts, **kw):
        """(typ, [data, ...]) = fetch(message_set, message_parts)
        Fetch (parts of) messages.
        'message_parts' should be a string of selected parts
        enclosed in parentheses, eg: "(UID BODY[TEXT])".
        'data' are tuples of message part envelope and data,
        followed by a string containing the trailer."""

        name = 'FETCH'
        kw['untagged_response'] = name
        return self._simple_command(name, message_set, message_parts, **kw)


    def getacl(self, mailbox, **kw):
        """(typ, [data]) = getacl(mailbox)
        Get the ACLs for a mailbox."""

        kw['untagged_response'] = 'ACL'
        return self._simple_command('GETACL', mailbox, **kw)


    def getannotation(self, mailbox, entry, attribute, **kw):
        """(typ, [data]) = getannotation(mailbox, entry, attribute)
        Retrieve ANNOTATIONs."""

        kw['untagged_response'] = 'ANNOTATION'
        return self._simple_command('GETANNOTATION', mailbox, entry, attribute, **kw)


    def getquota(self, root, **kw):
        """(typ, [data]) = getquota(root)
        Get the quota root's resource usage and limits.
        (Part of the IMAP4 QUOTA extension defined in rfc2087.)"""

        kw['untagged_response'] = 'QUOTA'
        return self._simple_command('GETQUOTA', root, **kw)


    def getquotaroot(self, mailbox, **kw):
        # Hmmm, this is non-std! Left for backwards-compatibility, sigh.
        # NB: usage should have been defined as:
        #   (typ, [QUOTAROOT responses...]) = getquotaroot(mailbox)
        #   (typ, [QUOTA responses...]) = response('QUOTA')
        """(typ, [[QUOTAROOT responses...], [QUOTA responses...]]) = getquotaroot(mailbox)
        Get the list of quota roots for the named mailbox."""

        typ, dat = self._simple_command('GETQUOTAROOT', mailbox)
        typ, quota = self._untagged_response(typ, dat, 'QUOTA')
        typ, quotaroot = self._untagged_response(typ, dat, 'QUOTAROOT')
        return self._deliver_dat(typ, [quotaroot, quota], kw)


    def idle(self, timeout=None, **kw):
        """"(typ, [data]) = idle(timeout=None)
        Put server into IDLE mode until server notifies some change,
        or 'timeout' (secs) occurs (default: 29 minutes),
        or another IMAP4 command is scheduled."""

        name = 'IDLE'
        self.literal = _IdleCont(self, timeout).process
        try:
            return self._simple_command(name, **kw)
        finally:
            self.state_change_pending.release()


    def list(self, directory='""', pattern='*', **kw):
        """(typ, [data]) = list(directory='""', pattern='*')
        List mailbox names in directory matching pattern.
        'data' is list of LIST responses.

	NB: for 'pattern':
	% matches all except separator ( so LIST "" "%" returns names at root)
	* matches all (so LIST "" "*" returns whole directory tree from root)"""

        name = 'LIST'
        kw['untagged_response'] = name
        return self._simple_command(name, directory, pattern, **kw)


    def login(self, user, password, **kw):
        """(typ, [data]) = login(user, password)
        Identify client using plaintext password.
        NB: 'password' will be quoted."""

        try:
            typ, dat = self._simple_command('LOGIN', user, self._quote(password))
            if typ != 'OK':
                self._deliver_exc(self.error, dat[-1], kw)
            self.state = AUTH
            if __debug__: self._log(1, 'state => AUTH')
        finally:
            self.state_change_pending.release()
        return self._deliver_dat(typ, dat, kw)


    def login_cram_md5(self, user, password, **kw):
        """(typ, [data]) = login_cram_md5(user, password)
        Force use of CRAM-MD5 authentication."""

        self.user, self.password = user, password
        return self.authenticate('CRAM-MD5', self._CRAM_MD5_AUTH, **kw)


    def _CRAM_MD5_AUTH(self, challenge):
        """Authobject to use with CRAM-MD5 authentication."""
        import hmac
        return self.user + " " + hmac.HMAC(self.password, challenge).hexdigest()


    def logout(self, **kw):
        """(typ, [data]) = logout()
        Shutdown connection to server.
        Returns server 'BYE' response."""

        self.state = LOGOUT
        if __debug__: self._log(1, 'state => LOGOUT')

        try:
            typ, dat = self._simple_command('LOGOUT')
        except:
            typ, dat = 'NO', ['%s: %s' % sys.exc_info()[:2]]
            if __debug__: self._log(1, dat)

        self._close_threads()

        self.state_change_pending.release()

        if __debug__: self._log(1, 'connection closed')

        bye = self.untagged_responses.get('BYE')
        if bye:
            typ, dat = 'BYE', bye
        return self._deliver_dat(typ, dat, kw)


    def lsub(self, directory='""', pattern='*', **kw):
        """(typ, [data, ...]) = lsub(directory='""', pattern='*')
        List 'subscribed' mailbox names in directory matching pattern.
        'data' are tuples of message part envelope and data."""

        name = 'LSUB'
        kw['untagged_response'] = name
        return self._simple_command(name, directory, pattern, **kw)


    def myrights(self, mailbox):
        """(typ, [data]) = myrights(mailbox)
        Show my ACLs for a mailbox (i.e. the rights that I have on mailbox)."""

        name = 'MYRIGHTS'
        kw['untagged_response'] = name
        return self._simple_command(name, mailbox, **kw)


    def namespace(self, **kw):
        """(typ, [data, ...]) = namespace()
        Returns IMAP namespaces ala rfc2342."""

        name = 'NAMESPACE'
        kw['untagged_response'] = name
        return self._simple_command(name, **kw)


    def noop(self, **kw):
        """(typ, [data]) = noop()
        Send NOOP command."""

        if __debug__: self._dump_ur(3)
        return self._simple_command('NOOP', **kw)


    def partial(self, message_num, message_part, start, length, **kw):
        """(typ, [data, ...]) = partial(message_num, message_part, start, length)
        Fetch truncated part of a message.
        'data' is tuple of message part envelope and data.
        NB: obsolete."""

        name = 'PARTIAL'
        kw['untagged_response'] = 'FETCH'
        return self._simple_command(name, message_num, message_part, start, length, **kw)


    def proxyauth(self, user, **kw):
        """(typ, [data]) = proxyauth(user)
        Assume authentication as 'user'.
        (Allows an authorised administrator to proxy into any user's mailbox.)"""

        try:
            return self._simple_command('PROXYAUTH', user, **kw)
        finally:
            self.state_change_pending.release()


    def rename(self, oldmailbox, newmailbox, **kw):
        """(typ, [data]) = rename(oldmailbox, newmailbox)
        Rename old mailbox name to new."""

        return self._simple_command('RENAME', oldmailbox, newmailbox, **kw)


    def search(self, charset, *criteria, **kw):
        """(typ, [data]) = search(charset, criterion, ...)
        Search mailbox for matching messages.
        'data' is space separated list of matching message numbers."""

        name = 'SEARCH'
        kw['untagged_response'] = name
        if charset:
            return self._simple_command(name, 'CHARSET', charset, *criteria, **kw)
        return self._simple_command(name, *criteria, **kw)


    def select(self, mailbox='INBOX', readonly=False, **kw):
        """(typ, [data]) = select(mailbox='INBOX', readonly=False)
        Select a mailbox. (Flushes all untagged responses.)
        'data' is count of messages in mailbox ('EXISTS' response).
        Mandated responses are ('FLAGS', 'EXISTS', 'RECENT', 'UIDVALIDITY'), so
        other responses should be obtained via "response('FLAGS')" etc."""

        self.commands_lock.acquire()
        self.untagged_responses = {}    # Flush old responses.
        self.commands_lock.release()

        self.is_readonly = readonly and True or False
        if readonly:
            name = 'EXAMINE'
        else:
            name = 'SELECT'
        try:
            rqb = self._command(name, mailbox)
            typ, dat = rqb.get_response('command: %s => %%s' % rqb.name)
            if typ != 'OK':
                if self.state == SELECTED:
                    self.state = AUTH
                if __debug__: self._log(1, 'state => AUTH')
                if typ == 'BAD':
                    self._deliver_exc(self.error, '%s command error: %s %s' % (name, typ, dat), kw)
                return self._deliver_dat(typ, dat, kw)
            self.state = SELECTED
            if __debug__: self._log(1, 'state => SELECTED')
        finally:
            self.state_change_pending.release()
        if 'READ-ONLY' in self.untagged_responses and not readonly:
            if __debug__: self._dump_ur(1)
            self._deliver_exc(self.readonly, '%s is not writable' % mailbox, kw)
        return self._deliver_dat(typ, self.untagged_responses.get('EXISTS', [None]), kw)


    def setacl(self, mailbox, who, what, **kw):
        """(typ, [data]) = setacl(mailbox, who, what)
        Set a mailbox acl."""

        try:
            return self._simple_command('SETACL', mailbox, who, what, **kw)
        finally:
            self.state_change_pending.release()


    def setannotation(self, *args, **kw):
        """(typ, [data]) = setannotation(mailbox[, entry, attribute]+)
        Set ANNOTATIONs."""

        kw['untagged_response'] = 'ANNOTATION'
        return self._simple_command('SETANNOTATION', *args, **kw)


    def setquota(self, root, limits, **kw):
        """(typ, [data]) = setquota(root, limits)
        Set the quota root's resource limits."""

        kw['untagged_response'] = 'QUOTA'
        try:
            return self._simple_command('SETQUOTA', root, limits, **kw)
        finally:
            self.state_change_pending.release()


    def sort(self, sort_criteria, charset, *search_criteria, **kw):
        """(typ, [data]) = sort(sort_criteria, charset, search_criteria, ...)
        IMAP4rev1 extension SORT command."""

        name = 'SORT'
        if (sort_criteria[0],sort_criteria[-1]) != ('(',')'):
            sort_criteria = '(%s)' % sort_criteria
        kw['untagged_response'] = name
        return self._simple_command(name, sort_criteria, charset, *search_criteria, **kw)


    def status(self, mailbox, names, **kw):
        """(typ, [data]) = status(mailbox, names)
        Request named status conditions for mailbox."""

        name = 'STATUS'
        kw['untagged_response'] = name
        return self._simple_command(name, mailbox, names, **kw)


    def store(self, message_set, command, flags, **kw):
        """(typ, [data]) = store(message_set, command, flags)
        Alters flag dispositions for messages in mailbox."""

        if (flags[0],flags[-1]) != ('(',')'):
            flags = '(%s)' % flags  # Avoid quoting the flags
        kw['untagged_response'] = 'FETCH'
        return self._simple_command('STORE', message_set, command, flags, **kw)


    def subscribe(self, mailbox, **kw):
        """(typ, [data]) = subscribe(mailbox)
        Subscribe to new mailbox."""

        try:
            return self._simple_command('SUBSCRIBE', mailbox, **kw)
        finally:
            self.state_change_pending.release()


    def thread(self, threading_algorithm, charset, *search_criteria, **kw):
        """(type, [data]) = thread(threading_alogrithm, charset, search_criteria, ...)
        IMAPrev1 extension THREAD command."""

        name = 'THREAD'
        kw['untagged_response'] = name
        return self._simple_command(name, threading_algorithm, charset, *search_criteria, **kw)


    def uid(self, command, *args, **kw):
        """(typ, [data]) = uid(command, arg, ...)
        Execute "command arg ..." with messages identified by UID,
            rather than message number.
        Assumes 'command' is legal in current state.
        Returns response appropriate to 'command'."""

        command = command.upper()
        if command in UID_direct:
            resp = command
        else:
            resp = 'FETCH'
        kw['untagged_response'] = resp
        return self._simple_command('UID', command, *args, **kw)


    def unsubscribe(self, mailbox, **kw):
        """(typ, [data]) = unsubscribe(mailbox)
        Unsubscribe from old mailbox."""

        try:
            return self._simple_command('UNSUBSCRIBE', mailbox, **kw)
        finally:
            self.state_change_pending.release()


    def xatom(self, name, *args, **kw):
        """(typ, [data]) = xatom(name, arg, ...)
        Allow simple extension commands notified by server in CAPABILITY response.
        Assumes extension command 'name' is legal in current state.
        Returns response appropriate to extension command 'name'."""

        name = name.upper()
        if not name in Commands:
            Commands[name] = ((self.state,), False)
        try:
            return self._simple_command(name, *args, **kw)
        finally:
            if self.state_change_pending.locked():
                self.state_change_pending.release()



    #       Internal methods


    def _append_untagged(self, typ, dat):

        if dat is None: dat = ''

        self.commands_lock.acquire()
        ur = self.untagged_responses.setdefault(typ, [])
        ur.append(dat)
        self.commands_lock.release()

        if __debug__: self._log(5, 'untagged_responses[%s] %s += ["%s"]' % (typ, len(ur)-1, dat))


    def _check_bye(self):

        bye = self.untagged_responses.get('BYE')
        if bye:
            raise self.abort(bye[-1])


    def _checkquote(self, arg):

        # Must quote command args if non-alphanumeric chars present,
        # and not already quoted.

        if not isinstance(arg, basestring):
            return arg
        if len(arg) >= 2 and (arg[0],arg[-1]) in (('(',')'),('"','"')):
            return arg
        if arg and self.mustquote_cre.search(arg) is None:
            return arg
        return self._quote(arg)


    def _command(self, name, *args, **kw):

        if Commands[name][CMD_VAL_ASYNC]:
            cmdtyp = 'async'
        else:
            cmdtyp = 'sync'

        if __debug__: self._log(1, '[%s] %s %s' % (cmdtyp, name, args))

        self.state_change_pending.acquire()

        self._end_idle()

        if cmdtyp == 'async':
            self.state_change_pending.release()
        else:
            # Need to wait for all async commands to complete
            self._check_bye()
            self.commands_lock.acquire()
            if self.tagged_commands:
                self.state_change_free.clear()
                need_event = True
            else:
                need_event = False
            self.commands_lock.release()
            if need_event:
                if __debug__: self._log(4, 'sync command %s waiting for empty commands Q' % name)
                self.state_change_free.wait()
                if __debug__: self._log(4, 'sync command %s proceeding' % name)

        if self.state not in Commands[name][CMD_VAL_STATES]:
            self.literal = None
            raise self.error('command %s illegal in state %s'
                                % (name, self.state))

        self._check_bye()

        self.commands_lock.acquire()
        for typ in ('OK', 'NO', 'BAD'):
            if typ in self.untagged_responses:
                del self.untagged_responses[typ]
        self.commands_lock.release()

        if 'READ-ONLY' in self.untagged_responses \
        and not self.is_readonly:
            self.literal = None
            raise self.readonly('mailbox status changed to READ-ONLY')

        if self.Terminate:
            raise self.abort('connection closed')

        rqb = self._request_push(name=name, **kw)

        data = '%s %s' % (rqb.tag, name)
        for arg in args:
            if arg is None: continue
            data = '%s %s' % (data, self._checkquote(arg))

        literal = self.literal
        if literal is not None:
            self.literal = None
            if isinstance(literal, str):
                literator = None
                data = '%s {%s}' % (data, len(literal))
            else:
                literator = literal

        rqb.data = '%s%s' % (data, CRLF)
        self.ouq.put(rqb)

        if literal is None:
            return rqb

        crqb = self._request_push(tag='continuation')

        while True:
            # Wait for continuation response

            ok, data = crqb.get_response('command: %s => %%s' % name)
            if __debug__: self._log(3, 'continuation => %s, %s' % (ok, data))

            # NO/BAD response?

            if not ok:
                break

            # Send literal

            if literator is not None:
                literal = literator(data, rqb)

            if literal is None:
                break

            if __debug__: self._log(4, 'write literal size %s' % len(literal))
            crqb.data = '%s%s' % (literal, CRLF)
            self.ouq.put(crqb)

            if literator is None:
                break

            self.commands_lock.acquire()
            self.tagged_commands['continuation'] = crqb
            self.commands_lock.release()

        return rqb


    def _command_complete(self, rqb, kw):

        # Called for non-callback commands

        typ, dat = rqb.get_response('command: %s => %%s' % rqb.name)
        self._check_bye()
        if typ == 'BAD':
            if __debug__: self._print_log()
            raise self.error('%s command error: %s %s' % (rqb.name, typ, dat))
        if 'untagged_response' in kw:
            return self._untagged_response(typ, dat, kw['untagged_response'])
        return typ, dat


    def _command_completer(self, (response, cb_arg, error)):

        # Called for callback commands
        rqb, kw = cb_arg
        rqb.callback = kw['callback']
        rqb.callback_arg = kw.get('cb_arg')
        if error is not None:
            if __debug__: self._print_log()
            typ, val = error
            rqb.abort(typ, val)
            return
        bye = self.untagged_responses.get('BYE')
        if bye:
            rqb.abort(self.abort, bye[-1])
            return
        typ, dat = response
        if typ == 'BAD':
            if __debug__: self._print_log()
            rqb.abort(self.error, '%s command error: %s %s' % (rqb.name, typ, dat))
            return
        if 'untagged_response' in kw:
            rqb.deliver(self._untagged_response(typ, dat, kw['untagged_response']))
        else:
            rqb.deliver(response)


    def _deliver_dat(self, typ, dat, kw):

        if 'callback' in kw:
            kw['callback'](((typ, dat), kw.get('cb_arg'), None))
        return typ, dat


    def _deliver_exc(self, exc, dat, kw):

        if 'callback' in kw:
            kw['callback']((None, kw.get('cb_arg'), (exc, dat)))
        raise exc(dat)


    def _end_idle(self):

        irqb = self.idle_rqb
        if irqb is not None:
            self.idle_rqb = None
            self.idle_timeout = None
            irqb.data = 'DONE%s' % CRLF
            self.ouq.put(irqb)
            if __debug__: self._log(2, 'server IDLE finished')


    def _match(self, cre, s):

        # Run compiled regular expression 'cre' match method on 's'.
        # Save result, return success.

        self.mo = cre.match(s)
        return self.mo is not None


    def _put_response(self, resp):

        if self._expecting_data > 0:
            rlen = len(resp)
            dlen = min(self._expecting_data, rlen)
            self._expecting_data -= dlen
            if rlen <= dlen:
                self._accumulated_data.append(resp)
                return
            self._accumulated_data.append(resp[:dlen])
            resp = resp[dlen:]

        if self._accumulated_data:
            typ, dat = self._literal_expected
            self._append_untagged(typ, (dat, ''.join(self._accumulated_data)))
            self._accumulated_data = []

        # Protocol mandates all lines terminated by CRLF
        resp = resp[:-2]

        if 'continuation' in self.tagged_commands:
            continuation_expected = True
        else:
            continuation_expected = False

        if self._literal_expected is not None:
            dat = resp
            if self._match(self.literal_cre, dat):
                self._literal_expected[1] = dat
                self._expecting_data = int(self.mo.group('size'))
                if __debug__: self._log(4, 'expecting literal size %s' % self._expecting_data)
                return
            typ = self._literal_expected[0]
            self._literal_expected = None
            self._append_untagged(typ, dat)  # Tail
            if __debug__: self._log(4, 'literal completed')
        else:
            # Command completion response?
            if self._match(self.tagre, resp):
                tag = self.mo.group('tag')
                typ = self.mo.group('type')
                dat = self.mo.group('data')
                if not tag in self.tagged_commands:
                    if __debug__: self._log(1, 'unexpected tagged response: %s' % resp)
                else:
                    self._request_pop(tag, (typ, [dat]))
            else:
                dat2 = None

                # '*' (untagged) responses?

                if not self._match(self.untagged_response_cre, resp):
                    if self._match(self.untagged_status_cre, resp):
                        dat2 = self.mo.group('data2')

                if self.mo is None:
                    # Only other possibility is '+' (continuation) response...

                    if self._match(self.continuation_cre, resp):
                        if not continuation_expected:
                            if __debug__: self._log(1, "unexpected continuation response: '%s'" % resp)
                            return
                        self._request_pop('continuation', (True, self.mo.group('data')))
                        return

                    if __debug__: self._log(1, "unexpected response: '%s'" % resp)
                    return

                typ = self.mo.group('type')
                dat = self.mo.group('data')
                if dat is None: dat = ''        # Null untagged response
                if dat2: dat = dat + ' ' + dat2

                # Is there a literal to come?

                if self._match(self.literal_cre, dat):
                    self._expecting_data = int(self.mo.group('size'))
                    if __debug__: self._log(4, 'read literal size %s' % self._expecting_data)
                    self._literal_expected = [typ, dat]
                    return

                self._append_untagged(typ, dat)

                if typ != 'OK':
                    self._end_idle()

        # Bracketed response information?

        if typ in ('OK', 'NO', 'BAD') and self._match(self.response_code_cre, dat):
            self._append_untagged(self.mo.group('type'), self.mo.group('data'))

        # Command waiting for aborted continuation response?

        if continuation_expected:
            self._request_pop('continuation', (False, resp))

        # Bad news?

        if typ in ('NO', 'BAD', 'BYE'):
            if typ == 'BYE':
                self.Terminate = True
            if __debug__: self._log(1, '%s response: %s' % (typ, dat))


    def _quote(self, arg):

        return '"%s"' % arg.replace('\\', '\\\\').replace('"', '\\"')


    def _request_pop(self, name, data):

        if __debug__: self._log(4, '_request_pop(%s, %s)' % (name, data))
        self.commands_lock.acquire()
        rqb = self.tagged_commands.pop(name)
        if not self.tagged_commands:
            self.state_change_free.set()
        self.commands_lock.release()
        rqb.deliver(data)


    def _request_push(self, tag=None, name=None, **kw):

        self.commands_lock.acquire()
        rqb = Request(self, name=name, **kw)
        if tag is None:
            tag = rqb.tag
        self.tagged_commands[tag] = rqb
        self.commands_lock.release()
        if __debug__: self._log(4, '_request_push(%s, %s, %s)' % (tag, name, `kw`))
        return rqb


    def _simple_command(self, name, *args, **kw):

        if 'callback' in kw:
            rqb = self._command(name, callback=self._command_completer, *args)
            rqb.callback_arg = (rqb, kw)
            return (None, None)
        return self._command_complete(self._command(name, *args), kw)


    def _untagged_response(self, typ, dat, name):

        if typ == 'NO':
            return typ, dat
        if not name in self.untagged_responses:
            return typ, [None]
        self.commands_lock.acquire()
        data = self.untagged_responses.pop(name)
        self.commands_lock.release()
        if __debug__: self._log(5, 'pop untagged_responses[%s] => %s' % (name, (typ, data)))
        return typ, data



    #       Threads


    def _close_threads(self):

        self.ouq.put(None)
        self.wrth.join()

        self.shutdown()

        self.rdth.join()
        self.inth.join()


    def _handler(self):

        threading.currentThread().setName('hdlr')

	time.sleep(0.1)	# Don't start handling before main thread ready

        if __debug__: self._log(1, 'starting')

        typ, val = self.abort, 'connection terminated'

        while not self.Terminate:
            try:
                if self.idle_timeout is not None:
                    timeout = self.idle_timeout - time.time()
                    if timeout <= 0:
                        timeout = 1
                    if __debug__:
                        if self.idle_rqb is not None:
                            self._log(5, 'server IDLING, timeout=%.2f' % timeout)
                else:
                    timeout = None
                line = self.inq.get(True, timeout)
            except Queue.Empty:
                if self.idle_rqb is None:
                    continue
                if self.idle_timeout > time.time():
                    continue
                if __debug__: self._log(2, 'server IDLE timedout')
                line = IDLE_TIMEOUT_RESPONSE

            if line is None:
                break

            if not isinstance(line, str):
                typ, val = line
                break

            try:
                self._put_response(line)
            except:
                typ, val = self.error, 'program error: %s - %s' % sys.exc_info()[:2]
                break

        self.Terminate = True

        while not self.ouq.empty():
            try:
                self.ouq.get_nowait().abort(typ, val)
            except Queue.Empty:
                break
        self.ouq.put(None)

        self.commands_lock.acquire()
        for name in self.tagged_commands.keys():
            rqb = self.tagged_commands.pop(name)
            rqb.abort(typ, val)
        self.state_change_free.set()
        self.commands_lock.release()

        if __debug__: self._log(1, 'finished')


    if hasattr(select_module, "poll"):

      def _reader(self):

        threading.currentThread().setName('redr')

        if __debug__: self._log(1, 'starting using poll')

        def poll_error(state):
            PollErrors = {
                select.POLLERR:     'Error',
                select.POLLHUP:     'Hang up',
                select.POLLNVAL:    'Invalid request: descriptor not open',
            }
            return ' '.join([PollErrors[s] for s in PollErrors.keys() if (s & state)])

        line_part = ''

        poll = select.poll()

        poll.register(self.read_fd, select.POLLIN)

        while not self.Terminate:
            if self.state == LOGOUT:
                timeout = 1
            else:
                timeout = None
            try:
                r = poll.poll(timeout)
                if __debug__: self._log(5, 'poll => %s' % `r`)
                if not r:
                    continue                                # Timeout

                fd,state = r[0]

                if state & select.POLLIN:
                    data = self.read(32768)                 # Drain ssl buffer if present
                    start = 0
                    dlen = len(data)
                    if __debug__: self._log(5, 'rcvd %s' % dlen)
                    if dlen == 0:
                        time.sleep(0.1)
                    while True:
                        stop = data.find('\n', start)
                        if stop < 0:
                            line_part += data[start:]
                            break
                        stop += 1
                        line_part, start, line = \
                            '', stop, line_part + data[start:stop]
                        if __debug__: self._log(4, '< %s' % line)
                        self.inq.put(line)

                if state & ~(select.POLLIN):
                    raise IOError(poll_error(state))
            except:
                reason = 'socket error: %s - %s' % sys.exc_info()[:2]
                if __debug__:
                    if not self.Terminate:
                        self._print_log()
                        if self.debug: self.debug += 4      # Output all
                        self._log(1, reason)
                self.inq.put((self.abort, reason))
                break

        poll.unregister(self.read_fd)

        if __debug__: self._log(1, 'finished')

    else:

      # No "poll" - use select()

      def _reader(self):

        threading.currentThread().setName('redr')

        if __debug__: self._log(1, 'starting using select')

        line_part = ''

        while not self.Terminate:
            if self.state == LOGOUT:
                timeout = 1
            else:
                timeout = None
            try:
                r,w,e = select.select([self.read_fd], [], [], timeout)
                if __debug__: self._log(5, 'select => %s, %s, %s' % (r,w,e))
                if not r:                                   # Timeout
                    continue

                data = self.read(32768)                     # Drain ssl buffer if present
                start = 0
                dlen = len(data)
                if __debug__: self._log(5, 'rcvd %s' % dlen)
                if dlen == 0:
                    time.sleep(0.1)
                while True:
                    stop = data.find('\n', start)
                    if stop < 0:
                        line_part += data[start:]
                        break
                    stop += 1
                    line_part, start, line = \
                        '', stop, line_part + data[start:stop]
                    if __debug__: self._log(4, '< %s' % line)
                    self.inq.put(line)
            except:
                reason = 'socket error: %s - %s' % sys.exc_info()[:2]
                if __debug__:
                    if not self.Terminate:
                        self._print_log()
                        if self.debug: self.debug += 4      # Output all
                        self._log(1, reason)
                self.inq.put((self.abort, reason))
                break

        if __debug__: self._log(1, 'finished')


    def _writer(self):

        threading.currentThread().setName('wrtr')

        if __debug__: self._log(1, 'starting')

        reason = 'Terminated'

        while not self.Terminate:
            rqb = self.ouq.get()
            if rqb is None:
                break   # Outq flushed

            try:
                self.send(rqb.data)
                if __debug__: self._log(4, '> %s' % rqb.data)
            except:
                reason = 'socket error: %s - %s' % sys.exc_info()[:2]
                if __debug__:
                    if not self.Terminate:
                        self._print_log()
                        if self.debug: self.debug += 4      # Output all
                        self._log(1, reason)
                rqb.abort(self.abort, reason)
                break

        self.inq.put((self.abort, reason))

        if __debug__: self._log(1, 'finished')



    #       Debugging


    if __debug__:

        def _init_debug(self, debug=None, debug_file=None):
            self.debug = debug is not None and debug or Debug is not None and Debug or 0
            self.debug_file = debug_file is not None and debug_file or sys.stderr

            self.debug_lock = threading.Lock()
            self._cmd_log_len = 20
            self._cmd_log_idx = 0
            self._cmd_log = {}           # Last `_cmd_log_len' interactions
            if self.debug:
                self._mesg('imaplib2 version %s' % __version__)
                self._mesg('imaplib2 debug level %s' % self.debug)


        def _dump_ur(self, lvl):
            if lvl > self.debug:
                return

            l = self.untagged_responses.items()
            if not l:
                return

            t = '\n\t\t'
            l = map(lambda x:'%s: "%s"' % (x[0], x[1][0] and '" "'.join(x[1]) or ''), l)
            self.debug_lock.acquire()
            self._mesg('untagged responses dump:%s%s' % (t, t.join(l)))
            self.debug_lock.release()


        def _log(self, lvl, line):
            if lvl > self.debug:
                return

            if line[-2:] == CRLF:
                line = line[:-2] + '\\r\\n'

            tn = threading.currentThread().getName()

            if self.debug >= 4:
                self.debug_lock.acquire()
                self._mesg(line, tn)
                self.debug_lock.release()
                return

            # Keep log of last `_cmd_log_len' interactions for debugging.
            self._cmd_log[self._cmd_log_idx] = (line, tn, time.time())
            self._cmd_log_idx += 1
            if self._cmd_log_idx >= self._cmd_log_len:
                self._cmd_log_idx = 0


        def _mesg(self, s, tn=None, secs=None):
            if secs is None:
                secs = time.time()
            if tn is None:
                tn = threading.currentThread().getName()
            tm = time.strftime('%M:%S', time.localtime(secs))
            self.debug_file.write('  %s.%02d %s %s\n' % (tm, (secs*100)%100, tn, s))
            self.debug_file.flush()


        def _print_log(self):
            self.debug_lock.acquire()
            i, n = self._cmd_log_idx, self._cmd_log_len
            if n: self._mesg('last %d imaplib2 reports:' % n)
            while n:
                try:
                    self._mesg(*self._cmd_log[i])
                except:
                    pass
                i += 1
                if i >= self._cmd_log_len:
                    i = 0
                n -= 1
            self.debug_lock.release()



class IMAP4_SSL(IMAP4):

    """IMAP4 client class over SSL connection

    Instantiate with:
        IMAP4_SSL(host=None, port=None, keyfile=None, certfile=None, debug=None, debug_file=None)

        host       - host's name (default: localhost);
        port       - port number (default: standard IMAP4 SSL port);
        keyfile    - PEM formatted file that contains your private key (default: None);
        certfile   - PEM formatted certificate chain file (default: None);
        debug      - debug level (default: 0 - no debug);
        debug_file - debug stream (default: sys.stderr).

    For more documentation see the docstring of the parent class IMAP4.
    """


    def __init__(self, host=None, port=None, keyfile=None, certfile=None, debug=None, debug_file=None):
        self.keyfile = keyfile
        self.certfile = certfile
        IMAP4.__init__(self, host, port, debug, debug_file)


    def open(self, host=None, port=None):
        """open(host=None, port=None)
        Setup secure connection to remote server on "host:port"
            (default: localhost:standard IMAP4 SSL port).
        This connection will be used by the routines:
            read, send, shutdown, socket, ssl."""

        self.host = host is not None and host or ''
        self.port = port is not None and port or IMAP4_SSL_PORT
        self.sock = self.open_socket()
        self.sslobj = socket.ssl(self.sock, self.keyfile, self.certfile)

        self.read_fd = self.sock.fileno()


    def read(self, size):
        """data = read(size)
        Read at most 'size' bytes from remote."""

        return self.sslobj.read(size)


    def send(self, data):
        """send(data)
        Send 'data' to remote."""

        # NB: socket.ssl needs a "sendall" method to match socket objects.
        bytes = len(data)
        while bytes > 0:
            sent = self.sslobj.write(data)
            if sent == bytes:
                break    # avoid copy
            data = data[sent:]
            bytes = bytes - sent


    def ssl(self):
        """ssl = ssl()
        Return socket.ssl instance used to communicate with the IMAP4 server."""

        return self.sslobj



class IMAP4_stream(IMAP4):

    """IMAP4 client class over a stream

    Instantiate with:
        IMAP4_stream(command, debug=None, debug_file=None)

        command    - string that can be passed to os.popen2();
        debug      - debug level (default: 0 - no debug);
        debug_file - debug stream (default: sys.stderr).

    For more documentation see the docstring of the parent class IMAP4.
    """


    def __init__(self, command, debug=None, debug_file=None):
        self.command = command
        self.host = command
        self.port = None
        self.sock = None
        self.writefile, self.readfile = None, None
        self.read_fd = None
        IMAP4.__init__(self, debug=debug, debug_file=debug_file)


    def open(self, host=None, port=None):
        """open(host=None, port=None)
        Setup a stream connection via 'self.command'.
        This connection will be used by the routines:
            read, send, shutdown, socket."""

        self.writefile, self.readfile = os.popen2(self.command)
        self.read_fd = self.readfile.fileno()


    def read(self, size):
        """Read 'size' bytes from remote."""

        return os.read(self.read_fd, size)


    def send(self, data):
        """Send data to remote."""

        self.writefile.write(data)
        self.writefile.flush()


    def shutdown(self):
        """Close I/O established in "open"."""

        self.readfile.close()
        self.writefile.close()



class _Authenticator(object):

    """Private class to provide en/de-coding
    for base64 authentication conversation."""

    def __init__(self, mechinst):
        self.mech = mechinst    # Callable object to provide/process data

    def process(self, data, rqb):
        ret = self.mech(self.decode(data))
        if ret is None:
            return '*'      # Abort conversation
        return self.encode(ret)

    def encode(self, inp):
        #
        #  Invoke binascii.b2a_base64 iteratively with
        #  short even length buffers, strip the trailing
        #  line feed from the result and append.  "Even"
        #  means a number that factors to both 6 and 8,
        #  so when it gets to the end of the 8-bit input
        #  there's no partial 6-bit output.
        #
        oup = ''
        while inp:
            if len(inp) > 48:
                t = inp[:48]
                inp = inp[48:]
            else:
                t = inp
                inp = ''
            e = binascii.b2a_base64(t)
            if e:
                oup = oup + e[:-1]
        return oup

    def decode(self, inp):
        if not inp:
            return ''
        return binascii.a2b_base64(inp)




class _IdleCont(object):

    """When process is called, server is in IDLE state
    and will send asynchronous changes."""

    def __init__(self, parent, timeout):
        self.parent = parent
        self.timeout = timeout is not None and timeout or IDLE_TIMEOUT
        self.parent.idle_timeout = self.timeout + time.time()

    def process(self, data, rqb):
        self.parent.idle_rqb = rqb
        self.parent.idle_timeout = self.timeout + time.time()
        if __debug__: self.parent._log(2, 'server IDLE started, timeout in %.2f secs' % self.timeout)
        return None



Mon2num = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}

InternalDate = re.compile(r'.*INTERNALDATE "'
    r'(?P<day>[ 0123][0-9])-(?P<mon>[A-Z][a-z][a-z])-(?P<year>[0-9][0-9][0-9][0-9])'
    r' (?P<hour>[0-9][0-9]):(?P<min>[0-9][0-9]):(?P<sec>[0-9][0-9])'
    r' (?P<zonen>[-+])(?P<zoneh>[0-9][0-9])(?P<zonem>[0-9][0-9])'
    r'"')


def Internaldate2Time(resp):

    """time_tuple = Internaldate2Time(resp)
    Convert IMAP4 INTERNALDATE to UT."""

    mo = InternalDate.match(resp)
    if not mo:
        return None

    mon = Mon2num[mo.group('mon')]
    zonen = mo.group('zonen')

    day = int(mo.group('day'))
    year = int(mo.group('year'))
    hour = int(mo.group('hour'))
    min = int(mo.group('min'))
    sec = int(mo.group('sec'))
    zoneh = int(mo.group('zoneh'))
    zonem = int(mo.group('zonem'))

    # INTERNALDATE timezone must be subtracted to get UT

    zone = (zoneh*60 + zonem)*60
    if zonen == '-':
        zone = -zone

    tt = (year, mon, day, hour, min, sec, -1, -1, -1)

    utc = time.mktime(tt)

    # Following is necessary because the time module has no 'mkgmtime'.
    # 'mktime' assumes arg in local timezone, so adds timezone/altzone.

    lt = time.localtime(utc)
    if time.daylight and lt[-1]:
        zone = zone + time.altzone
    else:
        zone = zone + time.timezone

    return time.localtime(utc - zone)

Internaldate2tuple = Internaldate2Time   # (Backward compatible)



def Time2Internaldate(date_time):

    """'"DD-Mmm-YYYY HH:MM:SS +HHMM"' = Time2Internaldate(date_time)
    Convert 'date_time' to IMAP4 INTERNALDATE representation."""

    if isinstance(date_time, (int, float)):
        tt = time.localtime(date_time)
    elif isinstance(date_time, (tuple, time.struct_time)):
        tt = date_time
    elif isinstance(date_time, str) and (date_time[0],date_time[-1]) == ('"','"'):
        return date_time        # Assume in correct format
    else:
        raise ValueError("date_time not of a known type")

    dt = time.strftime("%d-%b-%Y %H:%M:%S", tt)
    if dt[0] == '0':
        dt = ' ' + dt[1:]
    if time.daylight and tt[-1]:
        zone = -time.altzone
    else:
        zone = -time.timezone
    return '"' + dt + " %+03d%02d" % divmod(zone//60, 60) + '"'



FLAGS_cre = re.compile(r'.*FLAGS \((?P<flags>[^\)]*)\)')

def ParseFlags(resp):

    """('flag', ...) = ParseFlags(line)
    Convert IMAP4 flags response to python tuple."""

    mo = FLAGS_cre.match(resp)
    if not mo:
        return ()

    return tuple(mo.group('flags').split())



if __name__ == '__main__':

    # To test: invoke either as 'python imaplib2.py [IMAP4_server_hostname]',
    # or as 'python imaplib2.py -s "rsh IMAP4_server_hostname exec /etc/rimapd"'
    # or as 'python imaplib2.py -l "keyfile[:certfile]" [IMAP4_SSL_server_hostname]'

    import getopt, getpass

    try:
        optlist, args = getopt.getopt(sys.argv[1:], 'd:l:s:p:')
    except getopt.error, val:
        optlist, args = (), ()

    debug, port, stream_command, keyfile, certfile = (None,)*5
    for opt,val in optlist:
        if opt == '-d':
            debug = int(val)
        elif opt == '-l':
            try:
                keyfile,certfile = val.split(':')
            except ValueError:
                keyfile,certfile = val,val
        elif opt == '-p':
            port = int(val)
        elif opt == '-s':
            stream_command = val
            if not args: args = (stream_command,)

    if not args: args = ('',)
    if not port: port = (keyfile is not None) and IMAP4_SSL_PORT or IMAP4_PORT

    host = args[0]

    USER = getpass.getuser()

    test_mesg = 'From: %(user)s@localhost%(lf)sSubject: IMAP4 test%(lf)s%(lf)s%(data)s' \
			% {'user':USER, 'lf':'\n', 'data':open(__file__).read()}
    test_seq1 = [
    ('list', ('""', '%')),
    ('create', ('/tmp/imaplib2_test.0',)),
    ('rename', ('/tmp/imaplib2_test.0', '/tmp/imaplib2_test.1')),
    ('CREATE', ('/tmp/imaplib2_test.2',)),
    ('append', ('/tmp/imaplib2_test.2', None, None, test_mesg)),
    ('list', ('/tmp', 'imaplib2_test*')),
    ('select', ('/tmp/imaplib2_test.2',)),
    ('search', (None, 'SUBJECT', 'IMAP4 test')),
    ('fetch', ('1', '(FLAGS INTERNALDATE RFC822)')),
    ('store', ('1', 'FLAGS', '(\Deleted)')),
    ('namespace', ()),
    ('expunge', ()),
    ('recent', ()),
    ('close', ()),
    ]

    test_seq2 = (
    ('select', ()),
    ('response',('UIDVALIDITY',)),
    ('response', ('EXISTS',)),
    ('append', (None, None, None, test_mesg)),
    ('uid', ('SEARCH', 'SUBJECT', 'IMAP4 test')),
    ('uid', ('SEARCH', 'ALL')),
    ('uid', ('THREAD', 'references', 'UTF-8', '(SEEN)')),
    ('recent', ()),
    )

    AsyncError = None

    def responder((response, cb_arg, error)):
        global AsyncError
        cmd, args = cb_arg
        if error is not None:
            AsyncError = error
            M._mesg('[cb] ERROR %s %.100s => %s' % (cmd, args, error))
            return
        typ, dat = response
        M._mesg('[cb] %s %.100s => %s %.100s' % (cmd, args, typ, dat))
        if typ == 'NO':
            AsyncError = (Exception, dat[0])

    def run(cmd, args, cb=None):
        if AsyncError:
            M.logout()
            typ, val = AsyncError
            raise typ(val)
        M._mesg('%s %.100s' % (cmd, args))
        try:
            if cb is not None:
                typ, dat = getattr(M, cmd)(callback=responder, cb_arg=(cmd, args), *args)
                if M.debug:
                    M._mesg('%s %.100s => %s %.100s' % (cmd, args, typ, dat))
            else:
                typ, dat = getattr(M, cmd)(*args)
                M._mesg('%s %.100s => %s %.100s' % (cmd, args, typ, dat))
        except:
            M.logout()
            raise
        if typ == 'NO':
            M.logout()
            raise Exception(dat[0])
        return dat

    try:
        threading.currentThread().setName('main')

        if keyfile is not None:
            if not keyfile: keyfile = None
            if not certfile: certfile = None
            M = IMAP4_SSL(host=host, port=port, keyfile=keyfile, certfile=certfile, debug=debug)
        elif stream_command:
            M = IMAP4_stream(stream_command, debug=debug)
        else:
            M = IMAP4(host=host, port=port, debug=debug)
        if M.state != 'AUTH':   # Login needed
            PASSWD = getpass.getpass("IMAP password for %s on %s: " % (USER, host or "localhost"))
            test_seq1.insert(0, ('login', (USER, PASSWD)))
        M._mesg('PROTOCOL_VERSION = %s' % M.PROTOCOL_VERSION)
        M._mesg('CAPABILITIES = %r' % (M.capabilities,))

        for cmd,args in test_seq1:
            run(cmd, args, cb=1)

        for ml in run('list', ('/tmp/', 'imaplib2_test%')):
            mo = re.match(r'.*"([^"]+)"$', ml)
            if mo: path = mo.group(1)
            else: path = ml.split()[-1]
            run('delete', (path,), cb=1)

        for cmd,args in test_seq2:
            if (cmd,args) != ('uid', ('SEARCH', 'SUBJECT', 'IMAP4 test')):
                run(cmd, args, cb=1)
                continue

            dat = run(cmd, args)
            uid = dat[-1].split()
            if not uid: continue
            run('uid', ('FETCH', uid[-1],
                    '(FLAGS INTERNALDATE RFC822.SIZE RFC822.HEADER RFC822.TEXT)'), cb=1)
            run('uid', ('STORE', uid[-1], 'FLAGS', '(\Deleted)'), cb=1)
            run('expunge', (), cb=1)

        run('idle', (3,))
        run('logout', ())

        if debug:
            print
            M._print_log()

        print '\nAll tests OK.'

    except:
        print '\nTests failed.'

        if not debug:
            print '''
If you would like to see debugging output,
try: %s -d5
''' % sys.argv[0]

        raise
