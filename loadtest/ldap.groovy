import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import javax.naming.directory.*
import javax.naming.ldap.InitialLdapContext
import java.nio.file.Files
import java.nio.file.StandardOpenOption

// Only this JVM's LDAP contexts use this truststore. Never disable hostname checking.
def cfg = new JsonSlurper().parse(new File(props.getProperty('cutover.config')))
def runDir = new File(props.getProperty('cutover.run'))
def manifest = new JsonSlurper().parse(new File(runDir, 'manifest.json'))
def password = new File(cfg.password_file).getText('UTF-8').trim()
def userPassword = new File(runDir, 'user-password').getText('UTF-8').trim()
synchronized (props) {
    System.setProperty('javax.net.ssl.trustStore', cfg.truststore)
    System.setProperty('javax.net.ssl.trustStoreType', 'PKCS12')
    System.setProperty('javax.net.ssl.trustStorePassword', new File(cfg.truststore_pin_file).getText('UTF-8').trim())
}
def connect = { String url, String dn, String pw ->
    def env = new Hashtable<String, Object>()
    env.put('java.naming.factory.initial', 'com.sun.jndi.ldap.LdapCtxFactory')
    env.put('java.naming.provider.url', url)
    env.put('java.naming.security.authentication', 'simple')
    env.put('java.naming.security.principal', dn)
    env.put('java.naming.security.credentials', pw)
    env.put('com.sun.jndi.ldap.connect.timeout', cfg.timeout_ms.toString())
    env.put('com.sun.jndi.ldap.read.timeout', cfg.timeout_ms.toString())
    env.put('java.naming.referral', 'throw')
    env.put('com.sun.jndi.ldap.connect.pool', 'false')
    new InitialLdapContext(env, null)
}
def attrs = { Map values ->
    def a = new BasicAttributes(true)
    values.each { k, v ->
        def attr = new BasicAttribute(k)
        (v instanceof List ? v : [v]).each { attr.add(it.toString()) }
        a.put(attr)
    }
    a
}
def append = { String name, Map event ->
    synchronized (props) {
        Files.write(new File(runDir, name).toPath(), (JsonOutput.toJson(event) + '\n').getBytes('UTF-8'),
            StandardOpenOption.CREATE, StandardOpenOption.APPEND)
    }
}
def mode = props.getProperty('cutover.mode')
def root = manifest.test_dn
def userDN = { int i -> "uid=user-${i},${root}" }
def close = { c -> if (c != null) { try { c.close() } catch (Exception ignored) {} } }
def ldap = null
String writeDN = null
boolean acknowledged = false
try {
    if (mode == 'seed') {
        ldap = connect(cfg.proxy_url, cfg.bind_dn, password)
        ldap.createSubcontext(root, attrs([objectClass:['top','organizationalUnit'], ou:manifest.ou])).close()
        (0..<cfg.clients).each { i ->
            ldap.createSubcontext(userDN(i), attrs([objectClass:['top','inetOrgPerson'], uid:"user-${i}",
                cn:"Cutover user ${i}", sn:'Cutover', userPassword:userPassword])).close()
        }
        ldap.createSubcontext("cn=test-group,${root}", attrs([objectClass:['top','groupOfNames'],
            cn:'test-group', member:(0..<cfg.clients).collect { userDN(it) }])).close()
    } else if (mode == 'load') {
        int n = (vars.get('sequence') ?: '0').toInteger()
        vars.put('sequence', (n + 1).toString())
        int thread = ctx.getThreadNum()
        String op = ['search','bind','add'][n % 3]
        boolean persistent = thread % 2 == 0
        SampleResult.setSampleLabel("${op}-${persistent ? 'persistent' : 'fresh'}")
        if (op == 'bind') {
            // A bind always uses a new connection; no admin identity is reused for it.
            SampleResult.setSampleLabel('bind-fresh')
            ldap = connect(cfg.proxy_url, userDN(thread), userPassword)
        } else {
            ldap = persistent ? vars.getObject('ldap') : null
            if (ldap == null) {
                ldap = connect(cfg.proxy_url, cfg.bind_dn, password)
                if (persistent) vars.putObject('ldap', ldap)
            }
            if (op == 'search') {
                def result = ldap.getAttributes(userDN(thread), ['uid'] as String[])
                if (result.get('uid')?.get()?.toString() != "user-${thread}") {
                    throw new IllegalStateException('Search returned incorrect user')
                }
            } else {
                String id = UUID.randomUUID().toString()
                writeDN = "uid=op-${id},${root}"
                append('writes.jsonl', [dn:writeDN, acknowledged:false, timestamp:System.currentTimeMillis(), phase:'intent'])
                def created = ldap.createSubcontext(writeDN, attrs([objectClass:['top','inetOrgPerson'],
                    uid:"op-${id}", cn:id, sn:'Cutover', description:id]))
                acknowledged = true
                close(created)
            }
            if (persistent) ldap = null // owned by this thread until teardown
        }
    } else if (mode == 'reconcile') {
        def grouped = new File(runDir, 'writes.jsonl').readLines('UTF-8').findAll { it }.collect { new JsonSlurper().parseText(it) }.groupBy { it.dn }
        def events = grouped.collect { dn, records -> [dn:dn, acknowledged:records.any { it.acknowledged }] }
        def report = [checked_at:System.currentTimeMillis(), replicas:[], acknowledged:events.count { it.acknowledged },
            uncertain:events.count { !it.acknowledged }]
        cfg.replicas.each { replica ->
            def r = [name:replica.name, url:replica.url, acknowledged_missing:[], uncertain_present:[], errors:[]]
            def connection = null
            try {
                connection = connect(replica.url, cfg.bind_dn, password)
                events.each { event ->
                    try {
                        def found = connection.getAttributes(event.dn, ['description'] as String[])
                        if (found.get('description')?.get()?.toString() != event.dn.split('uid=op-')[1].split(',')[0]) {
                            r.errors.add([dn:event.dn, error:'wrong operation marker'])
                        } else if (!event.acknowledged) r.uncertain_present.add(event.dn)
                    } catch (javax.naming.NameNotFoundException missing) {
                        if (event.acknowledged) r.acknowledged_missing.add(event.dn)
                    } catch (Exception e) { r.errors.add([dn:event.dn, error:e.class.simpleName]) }
                }
            } catch (Exception e) { r.errors.add([error:e.class.simpleName]) }
            finally { close(connection) }
            report.replicas.add(r)
        }
        report.passed = report.acknowledged > 0 && report.replicas.size() == 6 &&
            report.replicas.every { !it.acknowledged_missing && !it.errors }
        new File(runDir, 'reconciliation.json').setText(JsonOutput.prettyPrint(JsonOutput.toJson(report)), 'UTF-8')
        if (!report.passed) throw new IllegalStateException('Reconciliation failed; see reconciliation.json')
    } else if (mode == 'cleanup') {
        ldap = connect(cfg.proxy_url, cfg.bind_dn, password)
        def controls = new SearchControls()
        controls.setSearchScope(SearchControls.ONELEVEL_SCOPE)
        controls.setReturningAttributes([] as String[])
        def results = ldap.search(root, '(objectClass=*)', controls)
        def dns = []
        try { while (results.hasMore()) dns.add(results.next().nameInNamespace) } finally { results.close() }
        dns.each { ldap.destroySubcontext(it) }
        ldap.destroySubcontext(root)
    } else if (mode == 'close') {
        close(vars.getObject('ldap'))
        vars.remove('ldap')
    } else { throw new IllegalArgumentException('Unknown test mode') }
    SampleResult.setSuccessful(true)
    SampleResult.setResponseCode('0')
    SampleResult.setResponseMessage('OK')
} catch (Exception e) {
    // Do not retry uncertain writes or hide connection failures.
    close(vars.getObject('ldap'))
    vars.remove('ldap')
    SampleResult.setSuccessful(false)
    SampleResult.setResponseCode(e.class.simpleName)
    SampleResult.setResponseMessage('LDAP operation failed; no automatic retry')
} finally {
    close(ldap)
    if (writeDN != null) append('writes.jsonl', [dn:writeDN, acknowledged:acknowledged, timestamp:System.currentTimeMillis()])
}
return null
