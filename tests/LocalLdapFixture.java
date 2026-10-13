// Local smoke-test fixture only. Never deploy this permissive server.
// Run with the UnboundID LDAP SDK and a disposable localhost PKCS12 certificate.
import com.unboundid.ldap.listener.*;
import com.unboundid.ldap.sdk.*;
import com.unboundid.util.ssl.*;
import java.net.InetAddress;
import java.nio.file.*;
import java.util.*;

public class LocalLdapFixture {
  public static void main(String[] args) throws Exception {
    String password = Files.readString(Path.of(args[1])).trim();
    SSLUtil ssl = new SSLUtil(new KeyStoreKeyManager(args[0], password.toCharArray(), "PKCS12", "test"), null);
    InMemoryDirectoryServerConfig config = new InMemoryDirectoryServerConfig("dc=example,dc=com");
    config.setSchema(null);
    config.addAdditionalBindCredentials("cn=administrator", password);
    List<InMemoryListenerConfig> listeners = new ArrayList<>();
    for (int port = 16360; port <= 16366; port++) {
      listeners.add(InMemoryListenerConfig.createLDAPSConfig("listener-" + port,
        InetAddress.getByName("127.0.0.1"), port, ssl.createSSLServerSocketFactory(), null));
    }
    config.setListenerConfigs(listeners);
    InMemoryDirectoryServer server = new InMemoryDirectoryServer(config);
    server.add(new Entry("dc=example,dc=com", new Attribute("objectClass", "top", "domain"), new Attribute("dc", "example")));
    server.startListening();
    System.out.println("LOCAL FIXTURE READY; seven endpoints share ONE in-memory database, not a replication test");
    Thread.sleep(Long.MAX_VALUE);
  }
}
