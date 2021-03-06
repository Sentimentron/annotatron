<template>
  <div class="form-container">
    <div class="form">
      <form v-on:submit.prevent="login">
        <div>
          <input v-model="username" type="text" placeholder="Username"/>
        </div>
        <div>
          <input v-model="password" type="password" placeholder="Password"/>
        </div>
        <div class="form-finalize">
          <button value="Login" type="submit" v-on:click="login()">Login</button>
          <p v-bind:class="state">{{promptText}}</p>
        </div>
      </form>
    </div>
  </div>

</template>

<script>
  import HTTP from '../http-common';
  import {EventBus} from '../global-bus';

  export default {
  name: 'login-form',
  data() {
    return {
      username: '',
      password: '',
      email: '',
      promptText: '',
      state: 'normal',
      bus: EventBus
    };
  },
  methods: {

    login() {
      const success = (response) => {
        // Called if authentication is successful
        const token = response.data.token;

        // Store the login token and stuff
        localStorage.setItem('Token', token);
        HTTP.defaults.headers.common['Authorization'] = "Token " + token;
        this.$Progress.set(70);

        // Trigger a call to a test location
        HTTP.get('v1/control/user').then( (response) => {
          if (response.data.username == this.username) {
            this.$Progress.finish();
            this.promptText = 'Welcome.';
            this.state = 'success';
            if (response.data.is_superuser) {
              this.bus.$emit('authenticationChanged', { authenticated: 'admin' });
            } else if (response.data.is_staff) {
              this.bus.$emit('authenticationChanged', { authenticated: 'staff' });
            } else {
              this.bus.$emit('authenticationChanged', { authenticated: 'annotator' });
            }
            this.$router.push({ name: 'Welcome', params: { userData: response.data } });
          } else {
            this.$Progress.fail();
            this.promptText = 'Security error.';
            console.log(response);
            this.state = 'error';
          }
        }).catch( (error) => {
          this.$Progress.fail();
          this.promptText = 'Something went wrong (couldn\'t finish logging in)';
          this.state = 'warning';
          console.log(error);
        });
      };

      const failure = (response) => {
        this.state = 'error';
        this.promptText = 'Something went wrong.';
        console.log(response);
        this.$Progress.fail();
      };

      const warning = (err) => {
        const error = err.response.data;
        for (const key in error) {
          if (key) {
            const problem = error[key];
            this.promptText = `${key}: ${problem}`;
          }
        }
        this.state = 'warning';
      };

      this.$Progress.start(5);
      this.promptText = 'Logging in...';
      this.state = 'working';
      HTTP.post('v1/auth/token', {
        username: this.username,
        password: this.password,
      }).then((response) => {
        if (response.status === 200) {
          success(response);
        } else {
          failure(response);
        }
      }).catch((error) => {
        console.log(error);
        if ('response' in error) {
          if (error.response.status === 400) {
            warning(error);
          }
        } else {
          failure(error);
        }
      }).finally( () => {
        this.password = '';
      });
    },

  },
};

</script>

<style scoped>

  .form {
    display: flex;
    flex-direction: column;
    background: #D8D8D8;
    border: 1px solid #979797;
    box-shadow: 2px 2px 3px 0 rgba(183, 183, 183, 0.50);
    width: 520px;
    padding: 15px;
  }

  .form-container {
    margin-top: 15px;
    display: flex;
    flex-direction: row;
    justify-content: center;
    text-align: left;
  }

  .form-finalize {
    display: flex;
  }

  .form-finalize > p {
    justify-content: right;
    padding-left: 30px;
  }

  .form > form > div {
    margin-top: 15px;
  }

  .form > h1 {
    margin-top: 0px;
    margin-bottom: 20px;
    padding-left: 5px;
  }

  .form > p {
    margin-top: 0px;
    margin-bottom: 12px;
    padding-left: 5px;
  }

  input {
    height: 50px;
    width: 500px;
    font-size: 20px;
    line-height: 75px;
    padding-left: 15px;
  }

  button {
    background-image: radial-gradient(50% 196%, #06B06D 50%, #04A163 100%);
    border-radius: 10px;
    padding: 15px;
    color: white;
    font-size: 20px;
    display: flex;
  }

</style>
