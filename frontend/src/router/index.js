import Vue from 'vue';
import Router from 'vue-router';
import Header from '@/components/Header';
import InitialSetupForm from '@/components/InitialSetupForm';
import LoginForm from '@/components/LoginForm';
import AuthenticatedWelcome from '@/components/AuthenticatedWelcome';
import CorporaView from '@/components/CorporaView';
import NewCorporaForm from '@/components/NewCorporaForm';

Vue.use(Router);

export default new Router({
  routes: [
    {
      path: '/',
      name: 'Header',
      component: Header,
      children: [{
        path: '/setup',
        name: 'InitialSetup',
        component: InitialSetupForm,
      }, {
        path: '/login',
        name: 'Login',
        component: LoginForm,
      }, {
        path: '/welcome',
        name: 'Welcome',
        component: AuthenticatedWelcome,
      }, {
        path: '/corpora',
        name: 'Corpora',
        component: CorporaView,
        children: [{
          path: 'add',
          name: 'AddCorpora',
          component: NewCorporaForm,
        }],
      }],
      meta: {
        progress: {
          func: [
            {call: 'color', modifier: 'temp', argument: '#ffb000'},
            {call: 'fail', modifier: 'temp', argument: '#6e0000'},
            {call: 'location', modifier: 'temp', argument: 'top'},
            {call: 'transition', modifier: 'temp', argument: {speed: '1.5s', opacity: '0.6s', termination: 400}}
          ]
        }
      }
    },
  ],

});
